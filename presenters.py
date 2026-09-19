import json
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")
RATING_LABELS = {
    "1": "매우 불만족",
    "2": "불만족",
    "3": "보통",
    "4": "만족",
    "5": "매우 만족",
}


def format_korean_datetime(value):
    """Render stored ISO timestamps as a readable Korea-time value."""
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(KST)
        return parsed.strftime("%Y.%m.%d %H:%M")
    except (TypeError, ValueError):
        return str(value)


def parse_multi_choice(value_json, value_text=None):
    if value_json:
        try:
            decoded = json.loads(value_json)
            if isinstance(decoded, list):
                return [str(item) for item in decoded if str(item).strip()]
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    if value_text:
        return [part.strip() for part in str(value_text).split(",") if part.strip()]
    return []


def feedback_summary(response_type, answers):
    """Convert stored feedback values into presentation-only labels and counts."""
    if response_type == "text":
        return [row["value_text"] for row in answers if row["value_text"]]

    values = []
    if response_type == "multi_choice":
        for row in answers:
            values.extend(parse_multi_choice(row["value_json"], row["value_text"]))
        counts = Counter(values)
        return [
            {"label": label, "count": count}
            for label, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    for row in answers:
        if row["value_text"]:
            values.append(str(row["value_text"]).strip())
    counts = Counter(values)
    return [
        {
            "label": f"{value}점 / 5점 ({RATING_LABELS.get(value, '만족도')})",
            "count": count,
        }
        for value, count in sorted(counts.items(), key=lambda item: item[0], reverse=True)
    ]
