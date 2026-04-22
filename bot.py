def format_list(users):
    return "\n".join([f"<@{u}>" for u in users]) if users else "—"

embed.description = (
    f"**Время по МСК:** {date_str} {time_str} МСК\n"
    f"**Локальное время:** {local_time}\n"
    f"**Коротко:** {short_time}\n\n"

    f"**Состав:** 1 танк / 2 хила / 7 дд\n"
    f"**Резерв:** до 5 человек, автоподнятие по классу\n\n"

    f"🛡️ **Танки [{len(data['tank'])}/1]**\n"
    f"{format_list(data['tank'])}\n"
    f"—\n"
    f"🌿 **Хилы [{len(data['heal'])}/2]**\n"
    f"{format_list(data['heal'])}\n"
    f"—\n"
    f"⚔️ **ДД [{len(data['dps'])}/7]**\n"
    f"{format_list(data['dps'])}\n\n"

    f"────────────\n\n"

    f"🛡️ **Резерв: Танки [{len(data['tank_res'])}]**\n"
    f"{format_list(data['tank_res'])}\n"
    f"—\n"
    f"🌿 **Резерв: Хилы [{len(data['heal_res'])}]**\n"
    f"{format_list(data['heal_res'])}\n"
    f"—\n"
    f"⚔️ **Резерв: ДД [{len(data['dps_res'])}]**\n"
    f"{format_list(data['dps_res'])}\n\n"

    f"**Как записаться**\n"
    f"Нажми одну из кнопок ниже.\n"
    f"Кнопки сверху — запись в основу.\n"
    f"Кнопки снизу — запись в резерв.\n"
    f"Кнопка ❌ Отмена убирает тебя из записи."
)
