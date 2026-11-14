from flask import url_for


MAIN_NAV = (
    ("Home", "home", "feed.home", ("feed",), "main"),
    ("Discover", "compass", "discover.index", ("discover",), "main"),
    ("Reels", "film", "reels.index", ("reels",), "main"),
    ("Live", "radio", "live.index", ("live",), "main"),
    ("LINKUP", "calendar", "linkup.index", ("linkup",), "main"),
    ("Messages", "message", "messages.inbox", ("messages",), "main"),
    ("Notifications", "bell", "notifications.index", ("notifications",), "main"),
    ("Games & Trivia", "layers", "games.hub", ("games", "trivia"), "main"),
    ("Schools", "school", "schools.index", ("schools",), "main"),
    ("Coins", "coin", "wallet.coins", ("wallet",), "main"),
    ("Profile", "user", None, ("profiles",), "main"),
    ("Settings", "sliders", "profiles.settings", ("profiles.settings",), "account"),
)

ADMIN_NAV = (
    ("Overview", "admin.dashboard", ("admin",)),
    ("Users", "admin.users", ()),
    ("Auto-approved (needs review)", "admin.auto_approved", ()),
    ("Developer card", "admin.developer_card", ()),
    ("Schools", "admin.schools", ()),
    ("Password resets", "admin.password_resets", ()),
    ("Reports", "admin.reports", ()),
    ("Media", "admin.media", ()),
    ("Payments", "admin.payments", ()),
    ("Loans", "admin.loans", ()),
    ("Live", "live.admin_live", ()),
    ("Trivia", "admin.trivia", ("admin.trivia_edit",)),
    ("Flags", "admin.flags", ()),
    ("Audit", "admin.audit_log", ()),
)


def main_navigation(user, endpoint, unread_messages=0, unread_notifications=0):
    """Build the same route-backed navigation links for desktop and mobile."""
    endpoint = endpoint or ""
    items = []
    for label, icon, target, active_endpoints, section in MAIN_NAV:
        if target is None:
            href = url_for("profiles.view", username=user["username"])
        else:
            href = url_for(target)
        item = {
            "label": label,
            "icon": icon,
            "href": href,
            "section": section,
            "active": (
                endpoint in active_endpoints or endpoint.split(".", 1)[0] in active_endpoints
                or endpoint.startswith(f"{target}.")
            ) if target else endpoint == "profiles.view",
            "badge": unread_messages if target == "messages.inbox" else unread_notifications if target == "notifications.index" else 0,
        }
        items.append(item)

    if user.get("role") in ("admin", "moderator"):
        target = "admin.dashboard" if user["role"] == "admin" else "admin.reports"
        items.insert(len(items) - 1, {
            "label": "Admin",
            "icon": "shield",
            "href": url_for(target),
            "section": "main",
            "active": endpoint.startswith("admin."),
            "badge": 0,
        })
    return items


def admin_navigation(user, endpoint):
    if user.get("role") not in ("admin", "moderator"):
        return []
    entries = ADMIN_NAV if user["role"] == "admin" else (
        ("Reports", "admin.reports", ()),
        ("Media", "admin.media", ()),
    )
    return [{
        "label": label,
        "href": url_for(target),
        "active": endpoint == target or endpoint in aliases,
    } for label, target, aliases in entries]
