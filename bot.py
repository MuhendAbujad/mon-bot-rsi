import io
import logging
import datetime
from telegram import Update, BotCommand, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from keep_alive import start_keep_alive
from rsi_monitor import get_analysis
from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    SYMBOL,
    INTERVAL,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    CHECK_INTERVAL_SECONDS,
    CHART_URL,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

MONITOR_JOB_NAME = "rsi_monitor"


def _signal_emoji(rsi: float, ob: float, os: float) -> str:
    if rsi >= ob:
        return "🔴"
    if rsi <= os:
        return "🟢"
    return "🟡"


def _signal_label(rsi: float, ob: float, os: float) -> str:
    if rsi >= ob:
        return f"OVERBOUGHT ≥ {ob} — SELL signal"
    if rsi <= os:
        return f"OVERSOLD ≤ {os} — BUY signal"
    return "NEUTRAL — no trade signal"


def _alert_message(rsi: float, ob: float, os: float, signal: str) -> str:
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    emoji = "🔴" if signal == "OVERBOUGHT" else "🟢"
    direction = "SELL signal" if signal == "OVERBOUGHT" else "BUY signal"
    threshold = f"≥ {ob}" if signal == "OVERBOUGHT" else f"≤ {os}"
    return (
        f"{emoji} <b>Ultimate RSI Alert — {SYMBOL}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📊 <b>Signal:</b> {signal} ({direction})\n"
        f"📈 <b>RSI:</b> <code>{rsi:.2f}</code> (threshold {threshold})\n"
        f"⏱ <b>Timeframe:</b> {INTERVAL} chart\n"
        f"🕐 <b>Time:</b> {now}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <i>This is an automated alert. Always apply your own risk management.</i>"
    )


async def monitor_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data

    if state.get("paused", False):
        logger.info("Monitoring is paused — skipping check.")
        return

    ob = state.get("rsi_overbought", RSI_OVERBOUGHT)
    os_ = state.get("rsi_oversold", RSI_OVERSOLD)

    data = await get_analysis()
    if data is None or data["rsi"] is None:
        logger.warning("Could not fetch RSI — skipping.")
        return

    rsi = data["rsi"]
    source = data.get("source", "?")
    now = datetime.datetime.utcnow().strftime("%H:%M:%S")
    logger.info(f"RSI = {rsi:.3f} (source: {source})")

    if rsi >= ob:
        signal = "OVERBOUGHT"
    elif rsi <= os_:
        signal = "OVERSOLD"
    else:
        signal = "NEUTRAL"
        state["last_signal"] = None
        return

    if signal != state.get("last_signal"):
        msg = _alert_message(rsi, ob, os_, signal)
        screenshot: bytes | None = data.get("screenshot_bytes")
        if screenshot:
            await context.bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=InputFile(io.BytesIO(screenshot), filename="chart.png"),
                caption=msg,
                parse_mode=ParseMode.HTML,
            )
        else:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=msg,
                parse_mode=ParseMode.HTML,
            )
        logger.info(f"✅ Alert sent: {signal} (RSI={rsi:.2f})")
        state["last_signal"] = signal

        history: list = state.setdefault("alert_history", [])
        history.append({
            "signal":    signal,
            "rsi":       rsi,
            "timestamp": datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        })
        if len(history) > 50:
            state["alert_history"] = history[-50:]
    else:
        logger.info(f"Signal unchanged ({signal}) — no duplicate alert.")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    ob = state.get("rsi_overbought", RSI_OVERBOUGHT)
    os_ = state.get("rsi_oversold", RSI_OVERSOLD)
    interval_s = state.get("interval_seconds", CHECK_INTERVAL_SECONDS)
    paused = state.get("paused", False)

    await update.message.reply_text("⏳ Scraping LuxAlgo Ultimate RSI from your TradingView chart… (may take up to 90s)")

    data = await get_analysis()
    if data is None or data["rsi"] is None:
        await update.message.reply_text(
            "❌ Could not read the RSI value from TradingView right now.\n"
            "Check that your TRADINGVIEW_SESSION_ID is valid and the LuxAlgo indicator is visible on the chart."
        )
        return

    rsi = data["rsi"]
    emoji = _signal_emoji(rsi, ob, os_)
    signal_label = _signal_label(rsi, ob, os_)
    fetched = data.get("fetched_at", "—")
    screenshot: bytes | None = data.get("screenshot_bytes")

    msg = (
        f"📡 <b>Bot Status — {SYMBOL} / {INTERVAL}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"{emoji} <b>LuxAlgo Ultimate RSI:</b> <code>{rsi:.2f}</code>\n"
        f"🎯 <b>Signal:</b> {signal_label}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"⚙️ <b>Settings</b>\n"
        f"  Overbought : RSI ≥ <code>{ob}</code>\n"
        f"  Oversold   : RSI ≤ <code>{os_}</code>\n"
        f"  Interval   : every <code>{interval_s}s</code>\n"
        f"  Monitoring : {'⏸ Paused' if paused else '▶️ Active'}\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>Fetched:</b> {fetched}"
    )

    if screenshot:
        await update.message.reply_photo(
            photo=InputFile(io.BytesIO(screenshot), filename="chart.png"),
            caption=msg,
            parse_mode=ParseMode.HTML,
        )
    else:
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    if state.get("paused", False):
        await update.message.reply_text("⏸ Bot is already paused. Use /resume to restart monitoring.")
        return
    state["paused"] = True
    await update.message.reply_text(
        "⏸ <b>Monitoring paused.</b>\nNo alerts will be sent until you use /resume.",
        parse_mode=ParseMode.HTML,
    )
    logger.info("Monitoring paused by Telegram command.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    if not state.get("paused", False):
        await update.message.reply_text("▶️ Bot is already running. Use /pause to pause it.")
        return
    state["paused"] = False
    state["last_signal"] = None
    await update.message.reply_text(
        "▶️ <b>Monitoring resumed.</b>\nAlerts are active again.",
        parse_mode=ParseMode.HTML,
    )
    logger.info("Monitoring resumed by Telegram command.")


async def cmd_setthreshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    args = context.args

    usage = (
        "Usage: /setthreshold &lt;overbought&gt; &lt;oversold&gt;\n"
        "Example: <code>/setthreshold 75 25</code>"
    )

    if len(args) != 2:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    try:
        ob = float(args[0])
        os_ = float(args[1])
    except ValueError:
        await update.message.reply_text(f"❌ Invalid values. {usage}", parse_mode=ParseMode.HTML)
        return

    if not (0 < os_ < ob < 100):
        await update.message.reply_text(
            "❌ Values must satisfy: <code>0 &lt; oversold &lt; overbought &lt; 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    state["rsi_overbought"] = ob
    state["rsi_oversold"]   = os_
    state["last_signal"] = None

    await update.message.reply_text(
        f"✅ <b>Thresholds updated</b>\n"
        f"  🔴 Overbought : RSI ≥ <code>{ob}</code>\n"
        f"  🟢 Oversold   : RSI ≤ <code>{os_}</code>",
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"Thresholds updated: OB={ob}, OS={os_}")


async def cmd_setinterval(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    args = context.args

    usage = (
        "Usage: /setinterval &lt;seconds&gt;\n"
        "Example: <code>/setinterval 30</code>\n"
        "Minimum: 10 seconds"
    )

    if len(args) != 1:
        await update.message.reply_text(usage, parse_mode=ParseMode.HTML)
        return

    try:
        seconds = int(args[0])
    except ValueError:
        await update.message.reply_text(f"❌ Must be a whole number. {usage}", parse_mode=ParseMode.HTML)
        return

    if seconds < 10:
        await update.message.reply_text("❌ Minimum interval is 10 seconds.", parse_mode=ParseMode.HTML)
        return

    for job in context.job_queue.get_jobs_by_name(MONITOR_JOB_NAME):
        job.schedule_removal()

    context.job_queue.run_repeating(
        monitor_job,
        interval=seconds,
        first=seconds,
        name=MONITOR_JOB_NAME,
    )
    state["interval_seconds"] = seconds

    await update.message.reply_text(
        f"✅ <b>Check interval updated</b>\nNow checking every <code>{seconds}</code> seconds.",
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"Interval updated to {seconds}s")


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    history: list = state.get("alert_history", [])

    if not history:
        await update.message.reply_text(
            "📭 <b>No alerts yet.</b>\nAlerts will appear here once the RSI crosses a threshold.",
            parse_mode=ParseMode.HTML,
        )
        return

    last10 = history[-10:][::-1]
    lines = []
    for i, entry in enumerate(last10, 1):
        emoji = "🔴" if entry["signal"] == "OVERBOUGHT" else "🟢"
        lines.append(
            f"{i}. {emoji} <b>{entry['signal']}</b>  RSI <code>{entry['rsi']:.2f}</code>\n"
            f"    🕐 {entry['timestamp']}"
        )

    total = len(history)
    msg = (
        f"📋 <b>Alert History — last {len(last10)} of {total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        + "\n\n".join(lines)
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_clearhistory(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = context.bot_data
    count = len(state.get("alert_history", []))
    if count == 0:
        await update.message.reply_text("📭 Alert history is already empty.")
        return
    state["alert_history"] = []
    await update.message.reply_text(
        f"🗑 <b>Alert history cleared.</b>\n{count} record(s) removed.",
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"Alert history cleared via Telegram ({count} records removed).")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "🤖 <b>Gold Trading Bot — Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "/status — Current RSI, price data &amp; settings\n"
        "/history — Last 10 alerts sent\n"
        "/clearhistory — Wipe the alert log\n"
        "/pause — Pause RSI monitoring &amp; alerts\n"
        "/resume — Resume monitoring\n"
        "/setthreshold &lt;OB&gt; &lt;OS&gt; — Set overbought/oversold levels\n"
        "  e.g. <code>/setthreshold 80 20</code>\n"
        "/setinterval &lt;seconds&gt; — Change check frequency\n"
        "  e.g. <code>/setinterval 30</code>\n"
        "/help — Show this message"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def on_startup(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("status",       "Current RSI, price & bot settings"),
        BotCommand("history",      "Last 10 alerts sent"),
        BotCommand("clearhistory", "Wipe the alert log"),
        BotCommand("pause",        "Pause monitoring & alerts"),
        BotCommand("resume",       "Resume monitoring"),
        BotCommand("setthreshold", "Set overbought/oversold levels"),
        BotCommand("setinterval",  "Change check frequency (seconds)"),
        BotCommand("help",         "Show all commands"),
    ])

    interval = app.bot_data.get("interval_seconds", CHECK_INTERVAL_SECONDS)
    app.job_queue.run_repeating(
        monitor_job,
        interval=interval,
        first=10,
        name=MONITOR_JOB_NAME,
    )

    ob  = app.bot_data.get("rsi_overbought", RSI_OVERBOUGHT)
    os_ = app.bot_data.get("rsi_oversold",   RSI_OVERSOLD)

    startup_msg = (
        f"🤖 <b>Gold Trading Bot Started</b>\n"
        f"Monitoring <b>{SYMBOL}</b> on <b>{INTERVAL}</b> chart.\n"
        f"Overbought: RSI ≥ {ob} | Oversold: RSI ≤ {os_}\n"
        f"Check interval: every {interval}s\n\n"
        f"Type /help to see all available commands."
    )
    await app.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=startup_msg,
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"Bot started — {SYMBOL} / {INTERVAL}, OB={ob}, OS={os_}, interval={interval}s")


def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.bot_data.update({
        "paused":          False,
        "last_signal":     None,
        "rsi_overbought":  RSI_OVERBOUGHT,
        "rsi_oversold":    RSI_OVERSOLD,
        "interval_seconds": CHECK_INTERVAL_SECONDS,
    })

    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("history",      cmd_history))
    app.add_handler(CommandHandler("clearhistory", cmd_clearhistory))
    app.add_handler(CommandHandler("pause",        cmd_pause))
    app.add_handler(CommandHandler("resume",       cmd_resume))
    app.add_handler(CommandHandler("setthreshold", cmd_setthreshold))
    app.add_handler(CommandHandler("setinterval",  cmd_setinterval))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("start",        cmd_help))

    start_keep_alive()
    logger.info("Starting polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
