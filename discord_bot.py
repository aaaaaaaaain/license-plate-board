# -*- coding: utf-8 -*-
"""Discord 機器人：在 Discord 頻道／私訊裡互動查詢車牌競標狀況。

用法（由 webapp.py 背景執行緒啟動）：
    bot = build_bot(get_state, prefix="!")
    bot.run(token)
"""

import asyncio
import io
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

logger = logging.getLogger("discord_bot")

# 監理站官方的車牌標售入口（真正要出價、繳費的地方；本機器人只做查詢）
MVDIS_URL = "https://www.mvdis.gov.tw/m3-emv/plate/index"


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


class PlatePicker(discord.ui.Select):
    """查詢結果下方的下拉選單：選一面號牌，就把它的出價趨勢圖貼出來。"""

    def __init__(self, view_ref):
        super().__init__(placeholder="📈 選一面號牌看趨勢圖", options=view_ref.pick_options(), row=1)
        self.view_ref = view_ref

    async def callback(self, interaction):
        item = self.view_ref.items[int(self.values[0])]
        await self.view_ref.on_pick(interaction, item)


class Paginator(discord.ui.View):
    """結果太多筆時，用「上一頁／下一頁」按鈕分頁瀏覽，而不是一次塞爆一則訊息。

    title 是每頁固定的抬頭（頁碼會接在後面），footer 是每頁都要重複的結論行
    （例如歷史查詢的「已決標」——它是整批紀錄的結果，不該只出現在某一頁）。

    另外給 items／on_pick 的話，訊息下方會多一個下拉選單，列出「這一頁」的項目
    （Discord 一個選單最多 25 個，剛好跟每頁筆數對得上），選了就呼叫 on_pick。
    """

    def __init__(self, lines, author_id, title, page_size=15, footer="", start_at_end=False,
                 items=None, on_pick=None, pick_label=None):
        super().__init__(timeout=120)
        self.lines = lines
        self.author_id = author_id
        self.title = title
        self.page_size = page_size
        self.footer = footer
        self.items = items or []
        self.on_pick = on_pick
        self.pick_label = pick_label
        self.max_page = max(0, (len(lines) - 1) // page_size)
        self.page = self.max_page if start_at_end else 0
        self.message = None
        self.picker = None
        if self.items and on_pick and pick_label:
            self.picker = PlatePicker(self)
            self.add_item(self.picker)
        self._update_buttons()

    def pick_options(self):
        start = self.page * self.page_size
        options = []
        for i in range(start, min(start + self.page_size, len(self.items))):
            label, desc = self.pick_label(self.items[i])
            options.append(discord.SelectOption(label=label[:100], description=desc[:100], value=str(i)))
        return options

    def _update_buttons(self):
        self.prev_btn.disabled = self.page <= 0
        self.next_btn.disabled = self.page >= self.max_page
        if self.picker:
            self.picker.options = self.pick_options()

    def render(self):
        start = self.page * self.page_size
        chunk = self.lines[start:start + self.page_size]
        header = f"{self.title}（第 {self.page + 1}/{self.max_page + 1} 頁）："
        text = header + "\n" + "\n".join(chunk)
        return text + ("\n" + self.footer if self.footer else "")

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


async def send_paginated(ctx, view):
    """只有一頁時就把翻頁鈕拿掉（兩顆都按不動，只是佔位）；選單還是留著。"""
    if view.max_page == 0:
        view.remove_item(view.prev_btn)
        view.remove_item(view.next_btn)
    if view.children:
        view.message = await ctx.send(view.render(), view=view)
    else:
        await ctx.send(view.render())


def build_bot(get_state, get_history=None, categories=None, prefix="!", get_history_filters=None,
              render_chart=None, get_public_url=None):
    categories = categories or []
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix=prefix, intents=intents, help_command=None)

    def iter_plates():
        state = get_state()
        for station in state.get("results", []):
            for p in station["plates"]:
                yield station, p

    async def off_loop(fn, *args):
        """把會阻塞的工作丟到背景執行緒跑，不要卡住 Discord 的事件迴圈。

        資料庫放在 /mnt/d（Windows 磁碟，走 9p），查一次要一秒上下——自動完成
        更是每打一個字就查一次。這些呼叫本來直接寫在 async 函式裡，等於整個
        事件迴圈被佔住，期間所有互動都來不及在 Discord 給的 3 秒內回應，
        使用者看到的就是指令永遠停在「命令發送中」。
        """
        return await asyncio.to_thread(fn, *args)

    async def send_chart(interaction, match):
        """下拉選單選了某一面號牌：把它的出價趨勢畫成 PNG 貼回頻道。

        畫圖前先 defer——Discord 只給互動 3 秒回應時間，查資料庫加畫圖雖然通常
        不到一秒，但掃描剛好在跑的時候資料庫會被鎖住等一下，先佔位比較保險。
        """
        st, p = match
        await interaction.response.defer()
        try:
            history, decided = await off_loop(
                get_history, p["號牌"], p["號牌類別"], st["section"], st["station"])
            if not history:
                await interaction.followup.send(f"「{p['號牌']}」還沒有出價紀錄，畫不出趨勢圖")
                return
            png = await off_loop(
                render_chart, p["號牌"], p["號牌類別"], st["section"], st["station"], history, decided)
        except Exception as e:
            logger.error(f"[discord] 產生趨勢圖失敗：{e}")
            await interaction.followup.send("產生趨勢圖失敗，請稍後再試")
            return
        filename = f"trend_{p['號牌'].replace('-', '')}.png"
        await interaction.followup.send(
            f"📈 {st['station']} {p['號牌']}（{p['號牌類別']}）目前 {p['目前出價']} 元・第 {p['出價次數']} 次出價",
            file=discord.File(io.BytesIO(png), filename=filename),
        )

    def _filter_choices(values, current, all_label, all_value="全部"):
        """自動完成選單：照使用者已經打的字過濾，並在最前面補一個「全部」選項。

        Discord 一次最多只能回 25 個選項，所以清單本身只取 24 個，留一格給「全部」。
        """
        current = (current or "").strip()
        matched = [v for v in values if current in v] if current else list(values)
        choices = [app_commands.Choice(name=v, value=v) for v in matched[:24]]
        if not current or "全部" in current:
            choices.insert(0, app_commands.Choice(name=all_label, value=all_value))
        return choices[:25]

    def presence_text():
        """機器人名字下面那行。

        只放數字、不放網址：個人資料卡那欄大約只顯示得下 30 個字，
        接上 trycloudflare 那種長網址一定會被截成「...」，兩邊都看不清楚。
        網址改用 /網址 指令拿（而且那裡是可以點的連結）。
        """
        state = get_state()
        total = sum(len(s["plates"]) for s in state.get("results", []))
        urgent = sum(1 for _, p in iter_plates() if p.get("is_urgent"))
        if urgent:
            return f"{total} 面競標中・{urgent} 面即將截止"
        return f"{total} 面競標中"

    # 每分鐘更新一次：掃描完數量會變，隧道重開後網址也會變（quick tunnel 每次都換一組），
    # 這裡固定重讀，所以重啟後不用手動改任何設定，狀態列自己就會跟上新網址。
    @tasks.loop(minutes=1)
    async def presence_loop():
        try:
            text = await off_loop(presence_text)
            await bot.change_presence(
                activity=discord.Activity(type=discord.ActivityType.watching, name=text)
            )
        except Exception as e:
            logger.error(f"[discord] 更新狀態失敗：{e}")

    @bot.event
    async def on_ready():
        logger.info(f"[discord] 已登入：{bot.user}")
        try:
            synced = await bot.tree.sync()
            logger.info(f"[discord] 已同步 {len(synced)} 個斜線指令（可能需要幾分鐘才會出現在 Discord）")
        except Exception as e:
            logger.error(f"[discord] 同步斜線指令失敗：{e}")
        # on_ready 斷線重連時會再觸發一次，重複 start 會拋例外
        if not presence_loop.is_running():
            presence_loop.start()
            logger.info(f"[discord] 狀態列已啟用：{presence_text()}")

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

        view = Paginator(
            lines, ctx.author.id, f"🔍 「{label}」共 {len(lines)} 面競標中",
            items=matches if render_chart and get_history else None,
            on_pick=send_chart,
            pick_label=lambda m: (
                f"{m[1]['號牌']}（{m[1]['號牌類別']}）",
                f"{m[0]['station']} · 目前 {m[1]['目前出價']} 元 · 第 {m[1]['出價次數']} 次",
            ),
        )
        await send_paginated(ctx, view)

    @search_cmd.autocomplete("station")
    async def station_autocomplete(interaction, current):
        return _filter_choices(sorted({st["station"] for st, _ in iter_plates()}), current, "全部監理站")

    @search_cmd.autocomplete("keyword")
    async def keyword_autocomplete(interaction, current):
        return _filter_choices(categories, current, "全部車種")

    @bot.hybrid_command(name="歷史", aliases=["history"], description="查詢某個號碼的出價歷史紀錄（同號碼換字首也會一起追蹤）")
    @app_commands.describe(
        plate="號牌或號碼，自行輸入，例如：PJY-1111 或 1111",
        category="車種，可從清單直接點選（可留空＝不限車種）",
        station="監理站，可從清單直接點選（可留空＝不限監理站）",
    )
    async def history_cmd(ctx, plate: str, category: str = "", station: str = ""):
        plate = plate.strip()
        category = category.strip()
        station = station.strip()
        if category == "全部":
            category = ""
        if station == "全部":
            station = ""

        history, decided = (
            await off_loop(get_history, plate, category or None, None, station or None)
            if get_history else ([], None)
        )
        scope = "、".join(filter(None, [category, station]))
        if not history:
            extra = f"（{scope}）" if scope else ""
            await ctx.send(f"「{plate}」{extra}目前沒有歷史紀錄（號碼可能打錯，或還沒有出價紀錄）")
            return

        title = f"📈 號碼 {plate}"
        if scope:
            title += f"（{scope}）"
        title += f" 出價歷史・共 {len(history)} 筆"

        # 沒指定監理站／車種時，同一個數字的紀錄可能混了好幾站、好幾種車種，
        # 每一行補上來源才看得懂價格為什麼忽高忽低；已經限定的那一項就不再重複印。
        lines = []
        for h in history:
            src = "".join(
                f"　{v}" for v in (
                    h.get("station") if not station else None,
                    h.get("category") if not category else None,
                ) if v
            )
            lines.append(f"{h['recorded_at']}　{h['plate']}{src}　{h['price']} 元（第 {h['bid_count']} 次出價）")

        footer = f"\n✅ 已決標：{decided['final_price']} 元（{decided['decided_at']}）" if decided else ""
        # 紀錄是由舊到新排的，但大家想先看到的是最新價格，所以直接翻到最後一頁
        view = Paginator(lines, ctx.author.id, title, page_size=10, footer=footer, start_at_end=True)
        await send_paginated(ctx, view)

    def _history_filters():
        # 歷史紀錄的監理站／車種清單來自資料庫（不是現在競標中的那份），
        # 拿不到就退回目前競標中的資料，選單至少不會整個空掉。
        if get_history_filters:
            try:
                data = get_history_filters()
                return data.get("stations") or [], data.get("categories") or []
            except Exception as e:
                logger.error(f"[discord] 取得歷史篩選清單失敗：{e}")
        return sorted({st["station"] for st, _ in iter_plates()}), list(categories)

    @history_cmd.autocomplete("station")
    async def history_station_autocomplete(interaction, current):
        stations, _ = await off_loop(_history_filters)
        return _filter_choices(stations, current, "全部監理站")

    @history_cmd.autocomplete("category")
    async def history_category_autocomplete(interaction, current):
        _, cats = await off_loop(_history_filters)
        return _filter_choices(cats, current, "全部車種")


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

    @bot.hybrid_command(name="網址", aliases=["url", "link"], description="取得看板的對外網址")
    async def url_cmd(ctx):
        url = await off_loop(get_public_url) if get_public_url else None
        if not url:
            await ctx.send("目前拿不到對外網址（隧道可能還沒啟動完成），稍等一下再試")
            return
        await ctx.send(f"🔗 看板網址：{url}\n（重開機後網址會換一組，這個指令回的永遠是最新的）")

    @bot.hybrid_command(name="官網", aliases=["mvdis", "official"], description="監理站官方車牌標售網頁")
    async def official_cmd(ctx):
        await ctx.send(f"🏛️ 監理站官方車牌標售網頁：{MVDIS_URL}\n（實際出價、繳費都要在官網操作，這個機器人只查詢競標狀況）")

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
            f"`{prefix}查詢 <車種> <號碼> <監理站>`：三個都可留空、也都可以只填一個，斜線指令可直接點選車種／監理站；"
            f"結果下方的下拉選單可以挑一面號牌，直接看它的出價趨勢圖\n"
            f"`{prefix}歷史 <號牌> <車種> <監理站>`：查詢某個號牌的出價歷史，車種／監理站可留空＝不限，"
            f"例如 `{prefix}歷史 PJY-1111`，斜線指令可直接點選車種／監理站\n"
            f"`{prefix}急件`：列出即將於截止門檻內的號牌\n"
            f"`{prefix}狀態`：目前競標中總數與最後更新時間\n"
            f"`{prefix}網址`：看板的對外網址（重開機後會換，這裡回最新的）\n"
            f"`{prefix}官網`：監理站官方標售網頁（要出價就是去那裡）\n"
        )

    return bot
