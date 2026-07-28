# -*- coding: utf-8 -*-
"""Discord 機器人：在 Discord 頻道／私訊裡互動查詢車牌競標狀況。

用法（由 webapp.py 背景執行緒啟動）：
    bot = build_bot(get_state, prefix="!")
    bot.run(token)
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger("discord_bot")


def format_remain(seconds):
    if seconds is None:
        return "-"
    if seconds < 0:
        return "已截止"
    d = int(seconds // 86400)
    h = int((seconds % 86400) // 3600)
    m = int((seconds % 3600) // 60)
    if d > 0:
        return f"{d} 天 {h} 小時"
    return f"{h} 小時 {m} 分" if h > 0 else f"{m} 分鐘"


class SearchPaginator(discord.ui.View):
    """查詢結果太多筆時，用「上一頁／下一頁」按鈕分頁瀏覽，而不是一次塞爆一則訊息。"""

    PAGE_SIZE = 15

    def __init__(self, keyword, lines, author_id):
        super().__init__(timeout=120)
        self.keyword = keyword
        self.lines = lines
        self.author_id = author_id
        self.page = 0
        self.max_page = max(0, (len(lines) - 1) // self.PAGE_SIZE)
        self.message = None
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.max_page

    def render(self):
        start = self.page * self.PAGE_SIZE
        chunk = self.lines[start:start + self.PAGE_SIZE]
        header = f"🔍 「{self.keyword}」共 {len(self.lines)} 面競標中（第 {self.page + 1}/{self.max_page + 1} 頁）："
        return header + "\n" + "\n".join(chunk)

    async def interaction_check(self, interaction):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("這不是你查詢的結果喔，自己下指令查一次吧", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    @discord.ui.button(label="◀ 上一頁", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction, button):
        self.page -= 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.render(), view=self)

    @discord.ui.button(label="下一頁 ▶", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction, button):
        self.page += 1
        self._update_buttons()
        await interaction.response.edit_message(content=self.render(), view=self)


def build_bot(get_state, get_history=None, categories=None, prefix="!"):
    categories = categories or []
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=prefix, intents=intents, help_command=None)

    def iter_plates():
        state = get_state()
        for station in state.get("results", []):
            for p in station["plates"]:
                yield station, p

    @bot.event
    async def on_ready():
        logger.info(f"[discord] 已登入：{bot.user}")
        try:
            synced = await bot.tree.sync()
            logger.info(f"[discord] 已同步 {len(synced)} 個斜線指令（可能需要幾分鐘才會出現在 Discord）")
        except Exception as e:
            logger.error(f"[discord] 同步斜線指令失敗：{e}")

    @bot.event
    async def on_command_error(ctx, error):
        if isinstance(error, commands.CommandNotFound):
            await ctx.send(f"沒有這個指令喔，如果是想查車種／號牌請用 `{prefix}查詢 關鍵字`，或輸入 `{prefix}help` 看說明")
            return
        logger.error(f"[discord] 指令錯誤：{error}")
        await ctx.send("指令執行時發生錯誤，請稍後再試")

    @bot.hybrid_command(name="查詢", aliases=["q", "search"], description="搜尋車種／監理站／號碼")
    @app_commands.describe(
        keyword="車種，可從清單直接點選（可留空）",
        number="想找的號碼或車牌，自行輸入，例如：8888 或 PJY-1111（可留空）",
        station="監理站，可從清單直接點選（可留空＝不限監理站）",
    )
    async def search_cmd(ctx, keyword: str = "", number: str = "", station: str = ""):
        keyword = keyword.strip()
        if keyword == "全部":
            keyword = ""
        number = number.strip()
        station = station.strip()
        if station == "全部":
            station = ""

        matches = []
        for st, p in iter_plates():
            if station and st["station"] != station:
                continue
            if keyword and keyword not in p["號牌類別"]:
                continue
            if number and number not in p["號牌"]:
                continue
            matches.append((st, p))

        label = "、".join(filter(None, [keyword, number, station])) or "全部"
        if not matches:
            await ctx.send(f"「{label}」目前未競標中")
            return

        lines = []
        for st, p in matches:
            mark = "⚠️ " if p.get("is_urgent") else ""
            lines.append(
                f"{mark}{st['section']} {st['station']} {p['號牌']}"
                f"（{p['號牌類別']}）目前 {p['目前出價']} 元，剩餘 {format_remain(p.get('seconds_left'))}"
            )

        view = SearchPaginator(label, lines, ctx.author.id)
        if view.max_page > 0:
            view.message = await ctx.send(view.render(), view=view)
        else:
            await ctx.send(view.render())

    @search_cmd.autocomplete("station")
    async def station_autocomplete(interaction, current):
        station_names = sorted({st["station"] for st, _ in iter_plates()})
        current = (current or "").strip()
        if current:
            station_names = [s for s in station_names if current in s]
        choices = [app_commands.Choice(name=s, value=s) for s in station_names[:24]]
        if not current or "全部" in current:
            choices.insert(0, app_commands.Choice(name="全部監理站", value="全部"))
        return choices[:25]

    @search_cmd.autocomplete("keyword")
    async def keyword_autocomplete(interaction, current):
        current = (current or "").strip()
        matched = [c for c in categories if current in c] if current else list(categories)
        choices = [app_commands.Choice(name=c, value=c) for c in matched[:24]]
        if not current or "全部" in current:
            choices.insert(0, app_commands.Choice(name="全部車種", value="全部"))
        return choices[:25]

    @bot.hybrid_command(name="歷史", aliases=["history"], description="查詢某個號碼的出價歷史紀錄（同號碼換字首也會一起追蹤）")
    @app_commands.describe(plate="號牌或號碼，例如：PJY-1111 或 1111", category="想限定車種可填，例如：普通重型機車（不填會列出所有車種）")
    async def history_cmd(ctx, plate: str, category: str = ""):
        plate = plate.strip()
        history, decided = get_history(plate, category.strip() or None) if get_history else ([], None)
        if not history:
            await ctx.send(f"「{plate}」目前沒有歷史紀錄（號碼可能打錯，或還沒有出價紀錄）")
            return
        lines = [f"📈 號碼 {plate} 出價歷史（共 {len(history)} 筆，最多顯示 10 筆）："]
        for h in history[-10:]:
            lines.append(f"{h['recorded_at']}　{h['plate']}　{h['price']} 元（第 {h['bid_count']} 次出價）")
        if decided:
            lines.append(f"\n✅ 已決標：{decided['final_price']} 元（{decided['decided_at']}）")
        await ctx.send("\n".join(lines))

    @bot.hybrid_command(name="急件", aliases=["urgent"], description="列出即將截止的號牌")
    async def urgent_cmd(ctx):
        urgent = [(s, p) for s, p in iter_plates() if p.get("is_urgent")]
        if not urgent:
            await ctx.send("目前沒有即將截止的號牌")
            return
        lines = [f"⚠️ 即將截止共 {len(urgent)} 面："]
        for station, p in urgent[:20]:
            lines.append(
                f"{station['section']} {station['station']} {p['號牌']}"
                f"（{p['號牌類別']}）剩餘 {format_remain(p.get('seconds_left'))}"
            )
        await ctx.send("\n".join(lines))

    @bot.hybrid_command(name="狀態", aliases=["status"], description="目前競標中總數與最後更新時間")
    async def status_cmd(ctx):
        state = get_state()
        total = sum(len(s["plates"]) for s in state.get("results", []))
        urgent_count = sum(1 for _, p in iter_plates() if p.get("is_urgent"))
        await ctx.send(
            f"📊 目前競標中：{total} 面（即將截止 {urgent_count} 面）\n"
            f"最後更新：{state.get('last_updated') or '尚未更新'}"
        )

    @bot.hybrid_command(name="help", aliases=["說明", "指令"], description="顯示可用指令說明")
    async def help_cmd(ctx):
        await ctx.send(
            "**可用指令**\n"
            f"`{prefix}查詢 <車種> <號碼> <監理站>`：三個都可留空、也都可以只填一個，斜線指令可直接點選車種／監理站\n"
            f"`{prefix}歷史 <號牌>`：查詢某個號牌的出價歷史，例如 `{prefix}歷史 PJY-1111`\n"
            f"`{prefix}急件`：列出即將於截止門檻內的號牌\n"
            f"`{prefix}狀態`：目前競標中總數與最後更新時間\n"
        )

    return bot
