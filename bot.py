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

    notified_30: bool = False
    notified_15: bool = False
    notified_start: bool = False

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
            "ended": self.ended,
            "notified_30": self.notified_30,
            "notified_15": self.notified_15,
            "notified_start": self.notified_start
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

    view = RaidView()

    if int(datetime.now(
        tz=MOSCOW_TZ
    ).timestamp()) >= raid.start_ts:

        for item in view.children:
            if isinstance(
                item,
                discord.ui.Button
            ):
                item.disabled = True

    await message.edit(
        embed=build_raid_embed(raid),
        view=view
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
            return

        now_ts = int(datetime.now(
            tz=MOSCOW_TZ
        ).timestamp())

        if now_ts >= raid.start_ts:
            await interaction.response.send_message(
                "Рейд уже начался.",
                ephemeral=True
            )
            return

        remove_everywhere(
            raid,
            interaction.user.id
        )

        if reserve:

            if len(
                raid.all_reserve_ids()
            ) >= MAX_RESERVE:

                await interaction.response.send_message(
                    "Резерв заполнен.",
                    ephemeral=True
                )
                return

            raid.reserve[role].append(
                interaction.user.id
            )

        else:

            if len(
                raid.main[role]
            ) >= ROLE_LIMITS[role]:

                await interaction.response.send_message(
                    "Слот занят.",
                    ephemeral=True
                )
                return

            raid.main[role].append(
                interaction.user.id
            )

        await refresh_raid_message(
            raid
        )

        await interaction.response.send_message(
            "Запись обновлена.",
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

        await refresh_raid_message(
            raid
        )

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
    date="Дата ДД.ММ.ГГГГ",
    time="Время ЧЧ:ММ"
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

    channel = interaction.channel

    if not isinstance(
        channel,
        discord.TextChannel
    ):
        return

    raid = RaidState(
        raid_id="temp",
        guild_id=interaction.guild.id,
        channel_id=channel.id,
        message_id=0,
        thread_id=None,
        title=title,
        start_ts=start_ts
    )

    message = await channel.send(
        embed=build_raid_embed(raid),
        view=RaidView()
    )

    thread = await message.create_thread(
        name=f"🗡️ {title}"
    )

    await thread.send(
        f"Обсуждение рейда "
        f"**{title}** открыто."
    )

    raid.raid_id = str(message.id)
    raid.message_id = message.id
    raid.thread_id = thread.id

    store.add(raid)

    await store.save()

    await interaction.response.send_message(
        "Рейд создан.",
        ephemeral=True
    )


@tasks.loop(minutes=1)
async def raid_tasks():

    now_ts = int(datetime.now(
        tz=MOSCOW_TZ
    ).timestamp())

    to_delete = []

    for raid_id, raid in store.raids.items():

        channel = bot.get_channel(
            raid.channel_id
        )

        if not isinstance(
            channel,
            discord.TextChannel
        ):
            continue

        all_users = list(set(
            raid.all_main_ids()
            + raid.all_reserve_ids()
        ))

        mentions = " ".join(
            f"<@{uid}>"
            for uid in all_users
        )

        # УВЕДОМЛЕНИЕ ЗА 30 МИН
        if (
            not raid.notified_30
            and now_ts >= raid.start_ts - 1800
        ):

            await channel.send(
                f"⏰ Рейд **{raid.title}** "
                f"через 30 минут.\n"
                f"{mentions}"
            )

            raid.notified_30 = True

        # УВЕДОМЛЕНИЕ ЗА 15 МИН
        if (
            not raid.notified_15
            and now_ts >= raid.start_ts - 900
        ):

            await channel.send(
                f"⚔️ Рейд **{raid.title}** "
                f"через 15 минут.\n"
                f"{mentions}"
            )

            raid.notified_15 = True

        # НАЧАЛО РЕЙДА
        if (
            not raid.notified_start
            and now_ts >= raid.start_ts
        ):

            await channel.send(
                f"🔥 Рейд **{raid.title}** "
                f"начался!\n"
                f"{mentions}"
            )

            raid.notified_start = True

            await refresh_raid_message(
                raid
            )

        # УДАЛЕНИЕ ЧЕРЕЗ 2 ЧАСА
        if now_ts >= raid.start_ts + 7200:

            try:
                message = await channel.fetch_message(
                    raid.message_id
                )

                await message.delete()

            except Exception:
                pass

            if raid.thread_id:

                thread = channel.guild.get_thread(
                    raid.thread_id
                )

                if thread:

                    try:
                        await thread.delete()

                    except Exception:
                        pass

            to_delete.append(
                raid_id
            )

    for raid_id in to_delete:
        store.raids.pop(
            raid_id,
            None
        )

    await store.save()


@bot.event
async def setup_hook():

    bot.add_view(
        RaidView()
    )

    guild_id = os.getenv(
        "GUILD_ID"
    )

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

    else:
        await bot.tree.sync()


@bot.event
async def on_ready():

    if bot.user:
        log.info(
            "Logged in as %s (%s)",
            bot.user,
            bot.user.id
        )

    if not raid_tasks.is_running():
        raid_tasks.start()


TOKEN = os.getenv(
    "DISCORD_TOKEN"
)

if not TOKEN:
    raise RuntimeError(
        "DISCORD_TOKEN is not set"
    )

bot.run(TOK
