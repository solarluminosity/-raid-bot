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

        self.raids = {
            raid_id: RaidState.from_dict(data)
            for raid_id, data in raw.items()
        }

        log.info("Loaded %s raids", len(self.raids))

    async def save(self) -> None:
        async with self.lock:
            payload = {
                raid_id: raid.to_dict()
                for raid_id, raid in self.raids.items()
            }

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

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


def format_user_list(user_ids: list[int]) -> str:
    return "\n".join(
        f"• <@{uid}>"
        for uid in user_ids
    ) if user_ids else "—"


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
            f"**Резерв:** до 5 человек\n\n"

            f"{ROLE_EMOJIS['tank']} "
            f"**Танки [{len(raid.main['tank'])}/{ROLE_LIMITS['tank']}]**\n"
            f"{format_user_list(raid.main['tank'])}\n"
            f"—\n"

            f"{ROLE_EMOJIS['heal']} "
            f"**Хилы [{len(raid.main['heal'])}/{ROLE_LIMITS['heal']}]**\n"
            f"{format_user_list(raid.main['heal'])}\n"
            f"—\n"

            f"{ROLE_EMOJIS['dd']} "
            f"**ДД [{len(raid.main['dd'])}/{ROLE_LIMITS['dd']}]**\n"
            f"{format_user_list(raid.main['dd'])}\n\n"

            f"────────────\n\n"

            f"{ROLE_EMOJIS['tank']} "
            f"**Резерв: Танки [{len(raid.reserve['tank'])}]**\n"
            f"{format_user_list(raid.reserve['tank'])}\n"
            f"—\n"

            f"{ROLE_EMOJIS['heal']} "
            f"**Резерв: Хилы [{len(raid.reserve['heal'])}]**\n"
            f"{format_user_list(raid.reserve['heal'])}\n"
            f"—\n"

            f"{ROLE_EMOJIS['dd']} "
            f"**Резерв: ДД [{len(raid.reserve['dd'])}]**\n"
            f"{format_user_list(raid.reserve['dd'])}\n\n"

            f"**Как записаться**\n"
            f"Нажми одну из кнопок ниже.\n"
            f"Кнопки сверху — запись в основу.\n"
            f"Кнопки снизу — запись в резерв.\n"
            f"Кнопка ❌ Отмена убирает тебя из записи."
        ),
    )

    status = "Завершён" if raid.ended else "Активен"

    embed.set_footer(
        text=(
            f"Статус: {status} • "
            f"Напоминания: за 60 мин, за 5 мин и в момент старта"
        )
    )

    return embed


def remove_everywhere(
    raid: RaidState,
    user_id: int
) -> tuple[bool, str | None]:

    for bucket_name in ("main", "reserve"):
        bucket = getattr(raid, bucket_name)

        for role, users in bucket.items():
            if user_id in users:
                users.remove(user_id)

                return (
                    True,
                    f"Ты убран(а) из "
                    f"{'основы' if bucket_name == 'main' else 'резерва'} "
                    f"({ROLE_LABELS[role]})."
                )

    return False, None


def can_join_main(
    raid: RaidState,
    role: str
) -> tuple[bool, str | None]:

    if raid.current_main_count() >= MAX_MAIN:
        return False, "Основа уже заполнена. Запишись в резерв."

    if len(raid.main[role]) >= ROLE_LIMITS[role]:
        return (
            False,
            f"Слот {ROLE_LABELS[role]} уже занят. "
            f"Запишись в резерв {ROLE_LABELS[role]}."
        )

    return True, None


def can_join_reserve(
    raid: RaidState,
    role: str
) -> tuple[bool, str | None]:

    if raid.current_reserve_count() >= MAX_RESERVE:
        return False, "Резерв уже заполнен."

    return True, None


async def refresh_raid_message(
    raid: RaidState,
    extra_thread_message: str | None = None
) -> None:

    channel = (
        bot.get_channel(raid.channel_id)
        or await bot.fetch_channel(raid.channel_id)
    )

    if not isinstance(channel, discord.TextChannel):
        return

    message = await channel.fetch_message(raid.message_id)

    await message.edit(
        embed=build_raid_embed(raid),
        view=RaidView()
    )

    if extra_thread_message and raid.thread_id:
        thread = bot.get_channel(raid.thread_id)

        if thread is None:
            try:
                thread = await bot.fetch_channel(raid.thread_id)
            except discord.HTTPException:
                thread = None

        if isinstance(thread, discord.Thread):
            await thread.send(
                extra_thread_message,
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    await store.save()


class RaidView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def handle_signup(
        self,
        interaction: discord.Interaction,
        role: str,
        reserve: bool
    ) -> None:

        if not interaction.message:
            await interaction.response.send_message(
                "Не удалось определить сообщение рейда.",
                ephemeral=True
            )
            return

        raid = store.get_by_message(interaction.message.id)

        if raid is None:
            await interaction.response.send_message(
                "Этот рейд уже удалён или не найден.",
                ephemeral=True
            )
            return

        if raid.ended:
            await interaction.response.send_message(
                "Рейд уже завершён.",
                ephemeral=True
            )
            return

        user_id = interaction.user.id

        remove_everywhere(raid, user_id)

        if reserve:
            ok, error = can_join_reserve(raid, role)

            if not ok:
                await store.save()

                await interaction.response.send_message(
                    error,
                    ephemeral=True
                )
                return

            raid.reserve[role].append(user_id)

            message = (
                f"Ты записан(а) в резерв "
                f"как {ROLE_LABELS[role]}."
            )

        else:
            ok, error = can_join_main(raid, role)

            if not ok:
                await store.save()

                await interaction.response.send_message(
                    error,
                    ephemeral=True
                )
                return

            raid.main[role].append(user_id)

            message = (
                f"Ты записан(а) в основу "
                f"как {ROLE_LABELS[role]}."
            )

        await refresh_raid_message(raid)

        await interaction.response.send_message(
            message,
            ephemeral=True
        )

    @discord.ui.button(
        label="Основа: Танк",
        emoji="🛡️",
        style=discord.ButtonStyle.primary,
        custom_id="raid:main:tank",
        row=0
    )
    async def main_tank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "tank", reserve=False)

    @discord.ui.button(
        label="Основа: Хил",
        emoji="🌿",
        style=discord.ButtonStyle.primary,
        custom_id="raid:main:heal",
        row=0
    )
    async def main_heal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "heal", reserve=False)

    @discord.ui.button(
        label="Основа: ДД",
        emoji="⚔️",
        style=discord.ButtonStyle.primary,
        custom_id="raid:main:dd",
        row=0
    )
    async def main_dd(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "dd", reserve=False)

    @discord.ui.button(
        label="Резерв: Танк",
        emoji="🛡️",
        style=discord.ButtonStyle.secondary,
        custom_id="raid:reserve:tank",
        row=1
    )
    async def reserve_tank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "tank", reserve=True)

    @discord.ui.button(
        label="Резерв: Хил",
        emoji="🌿",
        style=discord.ButtonStyle.secondary,
        custom_id="raid:reserve:heal",
        row=1
    )
    async def reserve_heal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "heal", reserve=True)

    @discord.ui.button(
        label="Резерв: ДД",
        emoji="⚔️",
        style=discord.ButtonStyle.secondary,
        custom_id="raid:reserve:dd",
        row=1
    )
    async def reserve_dd(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(interaction, "dd", reserve=True)

    @discord.ui.button(
        label="Отмена",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="raid:leave",
        row=2
    )
    async def leave(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not interaction.message:
            await interaction.response.send_message(
                "Не удалось определить сообщение рейда.",
                ephemeral=True
            )
            return

        raid = store.get_by_message(interaction.message.id)

        if raid is None:
            await interaction.response.send_message(
                "Этот рейд уже удалён или не найден.",
                ephemeral=True
            )
            return

        removed, message = remove_everywhere(
            raid,
            interaction.user.id
        )

        if not removed:
            await interaction.response.send_message(
                "Тебя нет в этом рейде.",
                ephemeral=True
            )
            return

        await refresh_raid_message(raid)

        await interaction.response.send_message(
            message,
            ephemeral=True
        )


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
        log.info(
            "Logged in as %s (%s)",
            bot.user,
            bot.user.id
        )


TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN is not set")

bot.run(TOKEN)
