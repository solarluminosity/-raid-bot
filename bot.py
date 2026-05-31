import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
log = logging.getLogger("raid_bot")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DATA_FILE = Path("raids.json")
MAX_MAIN = 10
MAX_RESERVE = 5
ROLE_LIMITS = {"tank": 1, "heal": 2, "dd": 7}
ROLE_LABELS = {"tank": "Танк", "heal": "Хил", "dd": "ДД"}
ROLE_EMOJIS = {"tank": "🛡️", "heal": "🌿", "dd": "⚔️"}
FIXED_ALERT_MINUTES = 5


@dataclass
class RaidState:
    raid_id: str
    guild_id: int
    channel_id: int
    message_id: int
    thread_id: int | None
    title: str
    start_ts: int
    main: dict[str, list[int]] = field(default_factory=lambda: {"tank": [], "heal": [], "dd": []})
    reserve: dict[str, list[int]] = field(default_factory=lambda: {"tank": [], "heal": [], "dd": []})
    sent_alerts: list[str] = field(default_factory=list)
    ended: bool = False
    cleanup_done: bool = False

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts, tz=MOSCOW_TZ)

    def all_main_ids(self) -> list[int]:
        return self.main["tank"] + self.main["heal"] + self.main["dd"]

    def all_reserve_ids(self) -> list[int]:
        return self.reserve["tank"] + self.reserve["heal"] + self.reserve["dd"]

    def mentions_main(self) -> str:
        ids = self.all_main_ids()
        return " ".join(f"<@{uid}>" for uid in ids) if ids else "—"

    def current_main_count(self) -> int:
        return len(self.all_main_ids())

    def current_reserve_count(self) -> int:
        return len(self.all_reserve_ids())

    def to_dict(self) -> dict[str, Any]:
        return {
            "raid_id": self.raid_id,
            "guild_id": self.guild_id,
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "title": self.title,
            "start_ts": self.start_ts,
            "main": self.main,
            "reserve": self.reserve,
            "sent_alerts": self.sent_alerts,
            "ended": self.ended,
            "cleanup_done": self.cleanup_done,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RaidState":
        return cls(**data)


class RaidStore:
    def __init__(self, path: Path):
        self.path = path
        self.raids: dict[str, RaidState] = {}
        self.lock = asyncio.Lock()
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            self.raids = {}
            return
        with self.path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        self.raids = {raid_id: RaidState.from_dict(data) for raid_id, data in raw.items()}
        log.info("Loaded %s raids", len(self.raids))

    async def save(self) -> None:
        async with self.lock:
            payload = {raid_id: raid.to_dict() for raid_id, raid in self.raids.items()}
            tmp_path = self.path.with_suffix(".tmp")
            with tmp_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            tmp_path.replace(self.path)

    def add(self, raid: RaidState) -> None:
        self.raids[raid.raid_id] = raid

    def remove(self, raid_id: str) -> None:
        self.raids.pop(raid_id, None)

    def get_by_message(self, message_id: int) -> RaidState | None:
        for raid in self.raids.values():
            if raid.message_id == message_id:
                return raid
        return None


store = RaidStore(DATA_FILE)
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def format_user_list(user_ids: list[int]) -> str:
    return "\n".join(f"• <@{uid}>" for uid in user_ids) if user_ids else "—"


def build_raid_embed(raid: RaidState) -> discord.Embed:
    local_full = f"<t:{raid.start_ts}:F>"
    local_short = f"<t:{raid.start_ts}:t>"
    msk_str = raid.start_dt.strftime("%d.%m.%Y %H:%M")

    embed = discord.Embed(
        title=f"Рейд: {raid.title}",
        color=discord.Color.blurple(),
        description=(
            f"**Время по МСК:** {msk_str} МСК\n"
            f"**Локальное время:** ({local_full})\n"
            f"**Коротко:** {msk_str.split()[1]} МСК ({local_short})\n\n"
            f"**Состав:** 1 танк / 2 хила / 7 дд\n"
            f"**Резерв:** до 5 человек, автоподнятие по классу\n\n"
            f"{ROLE_EMOJIS['tank']} **Танки [{len(raid.main['tank'])}/{ROLE_LIMITS['tank']}]**\n"
            f"{format_user_list(raid.main['tank'])}\n"
            f"—\n"
            f"{ROLE_EMOJIS['heal']} **Хилы [{len(raid.main['heal'])}/{ROLE_LIMITS['heal']}]**\n"
            f"{format_user_list(raid.main['heal'])}\n"
            f"—\n"
            f"{ROLE_EMOJIS['dd']} **ДД [{len(raid.main['dd'])}/{ROLE_LIMITS['dd']}]**\n"
            f"{format_user_list(raid.main['dd'])}\n\n"
            f"────────────\n\n"
            f"{ROLE_EMOJIS['tank']} **Резерв: Танки [{len(raid.reserve['tank'])}]**\n"
            f"{format_user_list(raid.reserve['tank'])}\n"
            f"—\n"
            f"{ROLE_EMOJIS['heal']} **Резерв: Хилы [{len(raid.reserve['heal'])}]**\n"
            f"{format_user_list(raid.reserve['heal'])}\n"
            f"—\n"
            f"{ROLE_EMOJIS['dd']} **Резерв: ДД [{len(raid.reserve['dd'])}]**\n"
            f"{format_user_list(raid.reserve['dd'])}\n\n"
            f"**Как записаться**\n"
            f"Нажми одну из кнопок ниже.\n"
            f"Кнопки сверху — запись в основу.\n"
            f"Кнопки снизу — запись в резерв.\n"
            f"Кнопка ❌ **Отмена** убирает тебя из записи."
        ),
    )

    status = "Завершён" if raid.ended else "Активен"
    embed.set_footer(
        text="Статус: "
        f"{status} • Напоминания: за 60 мин, за 5 мин и в момент старта"
    )
    return embed


def remove_everywhere(raid: RaidState, user_id: int) -> tuple[bool, str | None]:
    for bucket_name in ("main", "reserve"):
        bucket = getattr(raid, bucket_name)
        for role, users in bucket.items():
            if user_id in users:
                users.remove(user_id)
                return True, f"Ты убран(а) из {'основы' if bucket_name == 'main' else 'резерва'} ({ROLE_LABELS[role]})."
    return False, None


def can_join_main(raid: RaidState, role: str) -> tuple[bool, str | None]:
    if raid.current_main_count() >= MAX_MAIN:
        return False, "Основа уже заполнена. Запишись в резерв."
    if len(raid.main[role]) >= ROLE_LIMITS[role]:
        return False, f"Слот {ROLE_LABELS[role]} уже занят. Запишись в резерв {ROLE_LABELS[role]}."
    return True, None


def can_join_reserve(raid: RaidState, role: str) -> tuple[bool, str | None]:
    if raid.current_reserve_count() >= MAX_RESERVE:
        return False, "Резерв уже заполнен."
    return True, None


def promote_from_reserve(raid: RaidState) -> list[str]:
    moves: list[str] = []
    for role in ("tank", "heal", "dd"):
        while len(raid.main[role]) < ROLE_LIMITS[role] and raid.reserve[role]:
            user_id = raid.reserve[role].pop(0)
            if user_id not in raid.main[role]:
                raid.main[role].append(user_id)
                moves.append(f"<@{user_id}> автоматически поднят(а) из резерва в основу ({ROLE_LABELS[role]}).")
    return moves


async def refresh_raid_message(raid: RaidState, extra_thread_message: str | None = None) -> None:
    channel = bot.get_channel(raid.channel_id) or await bot.fetch_channel(raid.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    message = await channel.fetch_message(raid.message_id)
    await message.edit(embed=build_raid_embed(raid), view=RaidView())

    if extra_thread_message and raid.thread_id:
        thread = bot.get_channel(raid.thread_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(raid.thread_id)
            except discord.HTTPException:
                thread = None
        if isinstance(thread, discord.Thread):
            await thread.send(extra_thread_message, allowed_mentions=discord.AllowedMentions(users=True))

    await store.save()


class RaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_signup(self, interaction: discord.Interaction, role: str, reserve: bool) -> None:
        if not interaction.message:
            await interaction.response.send_message("Не удалось определить сообщение рейда.", ephemeral=True)
            return

        raid = store.get_by_message(interaction.message.id)
        if raid is None:
            await interaction.response.send_message("Этот рейд уже удалён или не найден.", ephemeral=True)
            return
        if raid.ended:
            await interaction.response.send_message("Рейд уже завершён.", ephemeral=True)
            return

        user_id = interaction.user.id
        remove_everywhere(raid, user_id)

        if reserve:
            ok, error = can_join_reserve(raid, role)
            if not ok:
                await store.save()
                await interaction.response.send_message(error, ephemeral=True)
                return
            raid.reserve[role].append(user_id)
            message = f"Ты записан(а) в резерв как {ROLE_LABELS[role]}."
        else:
            ok, error = can_join_main(raid, role)
            if not ok:
                await store.save()
                await interaction.response.send_message(error, ephemeral=True)
                return
            raid.main[role].append(user_id)
            message = f"Ты записан(а) в основу как {ROLE_LABELS[role]}."

        promotions = promote_from_reserve(raid)
        await refresh_raid_message(raid, "\n".join(promotions) if promotions else None)
        await interaction.response.send_message(message, ephemeral=True)

    @discord.ui.button(label="Основа: Танк", emoji="🛡️", style=discord.ButtonStyle.primary, custom_id="raid:main:tank", row=0)
    async def main_tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "tank", reserve=False)

    @discord.ui.button(label="Основа: Хил", emoji="🌿", style=discord.ButtonStyle.primary, custom_id="raid:main:heal", row=0)
    async def main_heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "heal", reserve=False)

    @discord.ui.button(label="Основа: ДД", emoji="⚔️", style=discord.ButtonStyle.primary, custom_id="raid:main:dd", row=0)
    async def main_dd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "dd", reserve=False)

    @discord.ui.button(label="Резерв: Танк", emoji="🛡️", style=discord.ButtonStyle.secondary, custom_id="raid:reserve:tank", row=1)
    async def reserve_tank(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "tank", reserve=True)

    @discord.ui.button(label="Резерв: Хил", emoji="🌿", style=discord.ButtonStyle.secondary, custom_id="raid:reserve:heal", row=1)
    async def reserve_heal(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "heal", reserve=True)

    @discord.ui.button(label="Резерв: ДД", emoji="⚔️", style=discord.ButtonStyle.secondary, custom_id="raid:reserve:dd", row=1)
    async def reserve_dd(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_signup(interaction, "dd", reserve=True)

    @discord.ui.button(label="Отмена", emoji="❌", style=discord.ButtonStyle.danger, custom_id="raid:leave", row=2)
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.message:
            await interaction.response.send_message("Не удалось определить сообщение рейда.", ephemeral=True)
            return

        raid = store.get_by_message(interaction.message.id)
        if raid is None:
            await interaction.response.send_message("Этот рейд уже удалён или не найден.", ephemeral=True)
            return

        removed, message = remove_everywhere(raid, interaction.user.id)
        if not removed:
            await interaction.response.send_message("Тебя нет в этом рейде.", ephemeral=True)
            return

        promotions = promote_from_reserve(raid)
        thread_message = "\n".join(promotions) if promotions else None
        await refresh_raid_message(raid, thread_message)
        await interaction.response.send_message(message, ephemeral=True)


@bot.event
async def setup_hook() -> None:
    bot.add_view(RaidView())
    guild_id = os.getenv("GUILD_ID")
    if guild_id:
        guild = discord.Object(id=int(guild_id))
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("Synced commands to guild %s", guild_id)
    else:
        await bot.tree.sync()
        log.info("Synced global commands")
    scheduler.start()


@bot.event
async def on_ready() -> None:
    if bot.user:
        log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.tree.command(name="raid_create", description="Создать запись на рейд")
@app_commands.describe(
    title="Название рейда",
    date="Дата в формате ДД.ММ.ГГГГ",
    time_msk="Время по МСК в формате ЧЧ:ММ",
)
async def raid_create(
    interaction: discord.Interaction,
    title: str,
    date: str,
    time_msk: str,
) -> None:
    raid_channel_id = os.getenv("RAID_CHANNEL_ID")
    if not raid_channel_id or not raid_channel_id.isdigit():
        await interaction.response.send_message(
            "Не настроен RAID_CHANNEL_ID в Railway.",
            ephemeral=True,
        )
        return

    try:
        target_channel_obj = bot.get_channel(int(raid_channel_id)) or await bot.fetch_channel(int(raid_channel_id))
    except discord.HTTPException:
        await interaction.response.send_message(
            "Не удалось найти канал для рейдов. Проверь RAID_CHANNEL_ID.",
            ephemeral=True,
        )
        return

    if not isinstance(target_channel_obj, discord.TextChannel):
        await interaction.response.send_message(
            "RAID_CHANNEL_ID должен указывать на текстовый канал.",
            ephemeral=True,
        )
        return

    target_channel = target_channel_obj

    try:
        start_dt = datetime.strptime(f"{date} {time_msk}", "%d.%m.%Y %H:%M").replace(tzinfo=MOSCOW_TZ)
    except ValueError:
        await interaction.response.send_message(
            "Неверный формат даты или времени. Используй дату `ДД.ММ.ГГГГ` и время `ЧЧ:ММ`.",
            ephemeral=True,
        )
        return

    if start_dt <= datetime.now(MOSCOW_TZ):
        await interaction.response.send_message("Дата и время рейда уже в прошлом по МСК.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    placeholder = await target_channel.send("Создаю рейд…")
    thread = await placeholder.create_thread(
        name=f"Обсуждение: {title}",
        auto_archive_duration=1440,
        reason=f"Raid thread for {title}",
    )

    raid = RaidState(
        raid_id=str(placeholder.id),
        guild_id=interaction.guild_id or 0,
        channel_id=target_channel.id,
        message_id=placeholder.id,
        thread_id=thread.id,
        title=title,
        start_ts=int(start_dt.timestamp()),
    )
    store.add(raid)

    await placeholder.edit(content=None, embed=build_raid_embed(raid), view=RaidView())
    await thread.send(
        f"Ветка для обсуждения рейда **{discord.utils.escape_markdown(title)}**.\n"
        f"Старт: {start_dt.strftime('%d.%m.%Y %H:%M')} МСК (<t:{raid.start_ts}:F>)."
    )
    await store.save()

    await interaction.followup.send(
        f"Готово. Рейд создан в {target_channel.mention}.",
        ephemeral=True,
    )


@bot.tree.command(name="raid_cancel", description="Удалить рейд и его ветку")
@app_commands.describe(message_id="ID сообщения рейда")
async def raid_cancel(interaction: discord.Interaction, message_id: str) -> None:
    raid = store.get_by_message(int(message_id)) if message_id.isdigit() else None
    if raid is None:
        await interaction.response.send_message("Рейд не найден.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    channel = bot.get_channel(raid.channel_id) or await bot.fetch_channel(raid.channel_id)
    if isinstance(channel, discord.TextChannel):
        try:
            message = await channel.fetch_message(raid.message_id)
            await message.delete()
        except discord.HTTPException:
            pass

    if raid.thread_id:
        try:
            thread = bot.get_channel(raid.thread_id) or await bot.fetch_channel(raid.thread_id)
            if isinstance(thread, discord.Thread):
                await thread.delete()
        except discord.HTTPException:
            pass

    store.remove(raid.raid_id)
    await store.save()
    await interaction.followup.send("Рейд удалён.", ephemeral=True)


async def send_and_autodelete(channel: discord.TextChannel, content: str) -> None:
    msg = await channel.send(content, allowed_mentions=discord.AllowedMentions(users=True))

    async def _delete_later() -> None:
        await asyncio.sleep(1800)
        try:
            await msg.delete()
        except discord.HTTPException:
            pass

    asyncio.create_task(_delete_later())


@tasks.loop(seconds=20)
async def scheduler() -> None:
    now = datetime.now(MOSCOW_TZ)
    to_remove: list[str] = []

    for raid in list(store.raids.values()):
        start_dt = raid.start_dt
        channel = bot.get_channel(raid.channel_id)
        if channel is None:
            try:
                fetched = await bot.fetch_channel(raid.channel_id)
                channel = fetched if isinstance(fetched, discord.TextChannel) else None
            except discord.HTTPException:
                channel = None
        if channel is None:
            continue

        alerts = {
            "60": start_dt - timedelta(hours=1),
            "5": start_dt - timedelta(minutes=FIXED_ALERT_MINUTES),
            "start": start_dt,
        }

        for key, alert_time in alerts.items():
            if key in raid.sent_alerts:
                continue
            if now >= alert_time:
                if key == "60":
                    text = f"⏰ **До рейда {raid.title} остался 1 час!**\nУчастники: {raid.mentions_main()}"
                elif key == "start":
                    text = f"🚨 **Рейд {raid.title} начинается прямо сейчас!**\nУчастники: {raid.mentions_main()}"
                else:
                    text = f"⚠️ **До рейда {raid.title} осталось 5 мин!**\nУчастники: {raid.mentions_main()}"

                await send_and_autodelete(channel, text)
                raid.sent_alerts.append(key)
                await store.save()

        if not raid.ended and now >= start_dt:
            raid.ended = True
            try:
                message = await channel.fetch_message(raid.message_id)
                await message.edit(embed=build_raid_embed(raid), view=RaidView())
            except discord.HTTPException:
                pass
            await store.save()

        if not raid.cleanup_done and now >= start_dt + timedelta(minutes=30):
            raid.cleanup_done = True
            try:
                message = await channel.fetch_message(raid.message_id)
                await message.delete()
            except discord.HTTPException:
                pass

            if raid.thread_id:
                try:
                    thread = bot.get_channel(raid.thread_id) or await bot.fetch_channel(raid.thread_id)
                    if isinstance(thread, discord.Thread):
                        await thread.delete()
                except discord.HTTPException:
                    pass

            to_remove.append(raid.raid_id)

    if to_remove:
        for raid_id in to_remove:
            store.remove(raid_id)
        await store.save()


@scheduler.before_loop
async def before_scheduler() -> None:
    await bot.wait_until_ready()


TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

bot.run(TOKEN)
