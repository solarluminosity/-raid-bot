import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

log = logging.getLogger("raid_bot")

MOSCOW_TZ = ZoneInfo("Europe/Moscow")
DATA_FILE = Path("raids.json")

MAX_MAIN = 10
MAX_RESERVE = 5

ROLE_LIMITS = {
    "tank": 1,
    "heal": 2,
    "dd": 7
}

ROLE_LABELS = {
    "tank": "Танк",
    "heal": "Хил",
    "dd": "ДД"
}

ROLE_EMOJIS = {
    "tank": "🛡️",
    "heal": "🌿",
    "dd": "⚔️"
}


@dataclass
class RaidState:
    raid_id: str
    guild_id: int
    channel_id: int
    message_id: int
    thread_id: int | None
    title: str
    start_ts: int

    main: dict[str, list[int]] = field(
        default_factory=lambda: {
            "tank": [],
            "heal": [],
            "dd": []
        }
    )

    reserve: dict[str, list[int]] = field(
        default_factory=lambda: {
            "tank": [],
            "heal": [],
            "dd": []
        }
    )

    ended: bool = False

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(
            self.start_ts,
            tz=MOSCOW_TZ
        )

    def all_main_ids(self) -> list[int]:
        return (
            self.main["tank"]
            + self.main["heal"]
            + self.main["dd"]
        )

    def all_reserve_ids(self) -> list[int]:
        return (
            self.reserve["tank"]
            + self.reserve["heal"]
            + self.reserve["dd"]
        )

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
            "ended": self.ended
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

            with self.path.open(
                "w",
                encoding="utf-8"
            ) as f:
                json.dump(
                    payload,
                    f,
                    ensure_ascii=False,
                    indent=2
                )

    def add(self, raid: RaidState) -> None:
        self.raids[raid.raid_id] = raid

    def get_by_message(
        self,
        message_id: int
    ) -> RaidState | None:

        for raid in self.raids.values():
            if raid.message_id == message_id:
                return raid

        return None


store = RaidStore(DATA_FILE)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


def format_user_list(user_ids: list[int]) -> str:
    if not user_ids:
        return "—"

    return "\n".join(
        f"• <@{uid}>"
        for uid in user_ids
    )


def build_raid_embed(
    raid: RaidState
) -> discord.Embed:

    local_full = f"<t:{raid.start_ts}:F>"
    local_short = f"<t:{raid.start_ts}:t>"

    msk_str = raid.start_dt.strftime(
        "%d.%m.%Y %H:%M"
    )

    embed = discord.Embed(
        title=f"Рейд: {raid.title}",
        color=discord.Color.blurple(),
        description=(
            f"**Время по МСК:** {msk_str} МСК\n"
            f"**Локальное время:** ({local_full})\n"
            f"**Коротко:** {local_short}\n\n"

            f"**Состав:** 1 танк / 2 хила / 7 дд\n"
            f"**Резерв:** до 5 человек\n\n"

            f"{ROLE_EMOJIS['tank']} "
            f"**Танки [{len(raid.main['tank'])}/1]**\n"
            f"{format_user_list(raid.main['tank'])}\n\n"

            f"{ROLE_EMOJIS['heal']} "
            f"**Хилы [{len(raid.main['heal'])}/2]**\n"
            f"{format_user_list(raid.main['heal'])}\n\n"

            f"{ROLE_EMOJIS['dd']} "
            f"**ДД [{len(raid.main['dd'])}/7]**\n"
            f"{format_user_list(raid.main['dd'])}\n\n"

            f"────────────\n\n"

            f"{ROLE_EMOJIS['tank']} "
            f"**Резерв: Танки**\n"
            f"{format_user_list(raid.reserve['tank'])}\n\n"

            f"{ROLE_EMOJIS['heal']} "
            f"**Резерв: Хилы**\n"
            f"{format_user_list(raid.reserve['heal'])}\n\n"

            f"{ROLE_EMOJIS['dd']} "
            f"**Резерв: ДД**\n"
            f"{format_user_list(raid.reserve['dd'])}"
        )
    )

    return embed


def remove_everywhere(
    raid: RaidState,
    user_id: int
):

    for bucket_name in ("main", "reserve"):
        bucket = getattr(raid, bucket_name)

        for role, users in bucket.items():
            if user_id in users:
                users.remove(user_id)
                return True

    return False


async def refresh_raid_message(
    raid: RaidState
):

    channel = bot.get_channel(
        raid.channel_id
    )

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        return

    message = await channel.fetch_message(
        raid.message_id
    )

    await message.edit(
        embed=build_raid_embed(raid),
        view=RaidView()
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
    ):

        if not interaction.message:
            return

        raid = store.get_by_message(
            interaction.message.id
        )

        if raid is None:
            await interaction.response.send_message(
                "Рейд не найден.",
                ephemeral=True
            )
            return

        remove_everywhere(
            raid,
            interaction.user.id
        )

        if reserve:
            if (
                raid.current_reserve_count()
                >= MAX_RESERVE
            ):
                await interaction.response.send_message(
                    "Резерв заполнен.",
                    ephemeral=True
                )
                return

            raid.reserve[role].append(
                interaction.user.id
            )

            text = (
                f"Ты записан(а) "
                f"в резерв как "
                f"{ROLE_LABELS[role]}."
            )

        else:
            if (
                len(raid.main[role])
                >= ROLE_LIMITS[role]
            ):
                await interaction.response.send_message(
                    "Слот занят.",
                    ephemeral=True
                )
                return

            raid.main[role].append(
                interaction.user.id
            )

            text = (
                f"Ты записан(а) "
                f"в основу как "
                f"{ROLE_LABELS[role]}."
            )

        await refresh_raid_message(raid)

        await interaction.response.send_message(
            text,
            ephemeral=True
        )

    @discord.ui.button(
        label="Основа: Танк",
        emoji="🛡️",
        style=discord.ButtonStyle.primary,
        custom_id="main_tank",
        row=0
    )
    async def main_tank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "tank",
            False
        )

    @discord.ui.button(
        label="Основа: Хил",
        emoji="🌿",
        style=discord.ButtonStyle.primary,
        custom_id="main_heal",
        row=0
    )
    async def main_heal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "heal",
            False
        )

    @discord.ui.button(
        label="Основа: ДД",
        emoji="⚔️",
        style=discord.ButtonStyle.primary,
        custom_id="main_dd",
        row=0
    )
    async def main_dd(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "dd",
            False
        )

    @discord.ui.button(
        label="Резерв: Танк",
        emoji="🛡️",
        style=discord.ButtonStyle.secondary,
        custom_id="reserve_tank",
        row=1
    )
    async def reserve_tank(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "tank",
            True
        )

    @discord.ui.button(
        label="Резерв: Хил",
        emoji="🌿",
        style=discord.ButtonStyle.secondary,
        custom_id="reserve_heal",
        row=1
    )
    async def reserve_heal(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "heal",
            True
        )

    @discord.ui.button(
        label="Резерв: ДД",
        emoji="⚔️",
        style=discord.ButtonStyle.secondary,
        custom_id="reserve_dd",
        row=1
    )
    async def reserve_dd(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):
        await self.handle_signup(
            interaction,
            "dd",
            True
        )

    @discord.ui.button(
        label="Отмена",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="leave_raid",
        row=2
    )
    async def leave_raid(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button
    ):

        if not interaction.message:
            return

        raid = store.get_by_message(
            interaction.message.id
        )

        if raid is None:
            return

        removed = remove_everywhere(
            raid,
            interaction.user.id
        )

        if not removed:
            await interaction.response.send_message(
                "Ты не записан(а).",
                ephemeral=True
            )
            return

        await refresh_raid_message(raid)

        await interaction.response.send_message(
            "Ты убран(а) из рейда.",
            ephemeral=True
        )


@bot.tree.command(
    name="raid_create",
    description="Создать рейд"
)
@app_commands.describe(
    title="Название рейда",
    date="Дата в формате ДД.ММ.ГГГГ",
    time="Время в формате ЧЧ:ММ"
)
async def raid_create(
    interaction: discord.Interaction,
    title: str,
    date: str,
    time: str
):

    try:
        dt = datetime.strptime(
            f"{date} {time}",
            "%d.%m.%Y %H:%M"
        ).replace(tzinfo=MOSCOW_TZ)

    except ValueError:
        await interaction.response.send_message(
            "Неверный формат даты.",
            ephemeral=True
        )
        return

    start_ts = int(dt.timestamp())

    embed = discord.Embed(
        title="Создание рейда...",
        color=discord.Color.blurple()
    )

    await interaction.response.send_message(
        embed=embed
    )

    message = await interaction.original_response()

    raid_id = str(message.id)

    raid = RaidState(
        raid_id=raid_id,
        guild_id=interaction.guild.id,
        channel_id=interaction.channel.id,
        message_id=message.id,
        thread_id=None,
        title=title,
        start_ts=start_ts
    )

    store.add(raid)

    await message.edit(
        embed=build_raid_embed(raid),
        view=RaidView()
    )

    await store.save()


@bot.event
async def setup_hook():

    bot.add_view(RaidView())

    guild_id = os.getenv("GUILD_ID")

    if guild_id:
        guild = discord.Object(
            id=int(guild_id)
        )

        bot.tree.copy_global_to(
            guild=guild
        )

        await bot.tree.sync(
            guild=guild
        )

        log.info(
            "Commands synced to guild"
        )

    else:
        await bot.tree.sync()

        log.info(
            "Global commands synced"
        )


@bot.event
async def on_ready():

    if bot.user:
        log.info(
            "Logged in as %s (%s)",
            bot.user,
            bot.user.id
        )


TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set"
    )

bot.run(TOKEN)
