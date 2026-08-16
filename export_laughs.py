#!/usr/bin/env python3
"""
Export iMessage Haha (laugh) stats for a group chat into laugh-data.json
for the local Laugh Board UI (index.html).

macOS only. Needs Full Disk Access for Terminal.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REACTION_TYPES = {
    "heart": 2000,
    "thumbs_up": 2001,
    "thumbs_down": 2002,
    "haha": 2003,
}
REACTION_KIND_BY_TYPE = {value: key for key, value in REACTION_TYPES.items()}
LAUGH_TYPE = REACTION_TYPES["haha"]
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
MESSAGES_DB = Path.home() / "Library" / "Messages" / "chat.db"
DEFAULT_COPY = Path("/tmp/imessage-laughs-chat-copy.db")
TZ = ZoneInfo("America/New_York")

CLIP_PATTERNS = [
    ("tiktok", re.compile(r"https?://(?:www\.)?(?:vm\.)?tiktok\.com/\S+", re.I)),
    ("instagram_reels", re.compile(r"https?://(?:www\.)?instagram\.com/(?:reel|reels)/\S+", re.I)),
    ("youtube_shorts", re.compile(r"https?://(?:www\.)?(?:youtube\.com/shorts/|youtu\.be/)\S+", re.I)),
]


def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def copy_db(src: Path, dest: Path) -> Path:
    if not src.exists():
        die(
            f"Messages database not found at {src}.\n"
            "Open the Messages app once and make sure iMessage is signed in on this Mac."
        )
    try:
        shutil.copy2(src, dest)
        for suffix in ("-wal", "-shm"):
            side = Path(str(src) + suffix)
            if side.exists():
                shutil.copy2(side, Path(str(dest) + suffix))
    except PermissionError:
        die(
            "Permission denied reading ~/Library/Messages/chat.db.\n\n"
            "Fix (one time):\n"
            "  1. System Settings → Privacy & Security → Full Disk Access\n"
            "  2. Turn on Terminal (or iTerm)\n"
            "  3. Quit Terminal completely and open it again\n"
            "  4. Re-run ./get-laughs.sh"
        )
    return dest


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def normalize_guid(associated_guid: str | None) -> str | None:
    if not associated_guid:
        return None
    g = associated_guid.strip()
    for prefix in ("p:0/", "p:1/", "bp:", "any:"):
        if g.startswith(prefix):
            g = g[len(prefix) :]
    if "/" in g and not g.startswith("iMessage"):
        parts = g.split("/")
        if len(parts[-1]) > 10:
            g = parts[-1]
    return g or None


def apple_to_dt(value) -> datetime | None:
    if value is None:
        return None
    d = int(value)
    if d > 1e15:
        sec = d / 1e9
    elif d > 1e11:
        sec = d / 1e3
    else:
        sec = float(d)
    return APPLE_EPOCH + timedelta(seconds=sec)


def load_names(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {str(k).strip().lower(): str(v) for k, v in data.items()}


def load_contacts_from_addressbook() -> dict[str, str]:
    """Read the macOS Contacts (AddressBook) database and return a mapping
    of phone/email → display name. Requires Full Disk Access for Terminal."""
    contacts: dict[str, str] = {}
    ab_dir = Path.home() / "Library" / "Application Support" / "AddressBook"
    if not ab_dir.exists():
        return contacts

    db_paths: list[Path] = []
    for match in glob.glob(str(ab_dir / "**" / "AddressBook-v22.abcddb"), recursive=True):
        db_paths.append(Path(match))
    if not db_paths:
        for match in glob.glob(str(ab_dir / "**" / "*.abcddb"), recursive=True):
            db_paths.append(Path(match))

    for db_path in db_paths:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row

            # Phone numbers
            try:
                rows = conn.execute("""
                    SELECT
                        COALESCE(r.ZFIRSTNAME, '') AS first,
                        COALESCE(r.ZLASTNAME, '') AS last,
                        p.ZFULLNUMBER AS phone
                    FROM ZABCDRECORD r
                    JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK
                    WHERE p.ZFULLNUMBER IS NOT NULL
                """).fetchall()
                for row in rows:
                    name = f"{row['first']} {row['last']}".strip()
                    if not name:
                        continue
                    phone = row["phone"]
                    digits = re.sub(r"\D", "", phone)
                    # Store multiple normalizations so matching is flexible
                    contacts[phone.lower()] = name
                    if len(digits) >= 10:
                        tail10 = digits[-10:]
                        contacts[f"+1{tail10}"] = name
                        contacts[tail10] = name
                    if digits:
                        contacts[f"+{digits}"] = name
            except sqlite3.OperationalError:
                pass

            # Email addresses
            try:
                rows = conn.execute("""
                    SELECT
                        COALESCE(r.ZFIRSTNAME, '') AS first,
                        COALESCE(r.ZLASTNAME, '') AS last,
                        e.ZADDRESS AS email
                    FROM ZABCDRECORD r
                    JOIN ZABCDEMAILADDRESS e ON e.ZOWNER = r.Z_PK
                    WHERE e.ZADDRESS IS NOT NULL
                """).fetchall()
                for row in rows:
                    name = f"{row['first']} {row['last']}".strip()
                    if not name:
                        continue
                    contacts[row["email"].strip().lower()] = name
            except sqlite3.OperationalError:
                pass

            conn.close()
        except (sqlite3.Error, OSError) as e:
            print(f"  (note: could not read contacts from {db_path.name}: {e})", file=sys.stderr)

    return contacts


def display_person(handle: str | None, is_from_me: bool, names: dict[str, str]) -> str:
    if is_from_me:
        return names.get("me", "Me")
    if not handle:
        return "Unknown"
    key = handle.strip().lower()
    if key in names:
        return names[key]
    digits = re.sub(r"\D", "", handle)
    if len(digits) >= 10:
        tail = digits[-10:]
        for nk, nv in names.items():
            nd = re.sub(r"\D", "", nk)
            if nd.endswith(tail):
                return nv
    # Keep the full handle so the local UI can map each phone/email to a name.
    # This data never leaves the user's machine unless they share the export.
    return handle


def resolve_chat_contact(chat: dict, contact_names: dict[str, str]) -> str | None:
    """Try to resolve a chat's identifier to a contact name."""
    if not contact_names:
        return None
    ident = (chat.get("chat_identifier") or "").strip().lower()
    if not ident:
        return None
    resolved = contact_names.get(ident)
    if resolved:
        return resolved
    digits = re.sub(r"\D", "", ident)
    if len(digits) >= 10:
        resolved = contact_names.get(f"+1{digits[-10:]}")
        if resolved:
            return resolved
        resolved = contact_names.get(digits[-10:])
        if resolved:
            return resolved
    return None


def list_chats(conn: sqlite3.Connection, limit: int = 80, min_participants: int = 2) -> list[dict]:
    rows = conn.execute(
        """
        SELECT
          c.ROWID AS chat_id,
          COALESCE(NULLIF(c.display_name, ''), c.chat_identifier) AS display_name,
          c.chat_identifier AS chat_identifier,
          (SELECT COUNT(*) FROM chat_handle_join chj WHERE chj.chat_id = c.ROWID) AS participants,
          (
            SELECT COUNT(*)
            FROM chat_message_join cmj
            JOIN message m ON m.ROWID = cmj.message_id
            WHERE cmj.chat_id = c.ROWID
              AND (m.associated_message_type IS NULL OR m.associated_message_type = 0)
          ) AS message_count
        FROM chat c
        WHERE (
            SELECT COUNT(*) FROM chat_handle_join chj WHERE chj.chat_id = c.ROWID
          ) >= ?
           OR COALESCE(c.display_name, '') != ''
           OR c.chat_identifier LIKE 'chat%'
        ORDER BY message_count DESC
        LIMIT ?
        """,
        (min_participants, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def fetch_messages(conn: sqlite3.Connection, chat_ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(chat_ids))
    return conn.execute(
        f"""
        SELECT
          m.ROWID AS rowid,
          m.guid AS guid,
          m.text AS text,
          m.is_from_me AS is_from_me,
          m.date AS date,
          m.associated_message_type AS associated_message_type,
          m.associated_message_guid AS associated_message_guid,
          h.id AS handle,
          cmj.chat_id AS chat_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        LEFT JOIN handle h ON h.ROWID = m.handle_id
        WHERE cmj.chat_id IN ({placeholders})
        ORDER BY m.date ASC, m.ROWID ASC
        """,
        chat_ids,
    ).fetchall()


def is_normal_message(row: sqlite3.Row) -> bool:
    t = row["associated_message_type"]
    return t is None or int(t) == 0


def clip_kinds(text: str | None) -> list[str]:
    if not text:
        return []
    found = []
    for kind, pat in CLIP_PATTERNS:
        if pat.search(text):
            found.append(kind)
    return found


def first_clip_url(text: str | None) -> str | None:
    if not text:
        return None
    for _, pat in CLIP_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(0).rstrip(").,]}>\"'")
    return None


def person_stats_empty() -> dict:
    return {
        "messages_sent": 0,
        "characters_sent": 0,
        "laughs_given": 0,
        "laughs_received": 0,
    }


def finalize_people(stats: dict[str, dict], people_order: list[str]) -> list[dict]:
    out = []
    for p in people_order:
        s = stats.get(p, person_stats_empty())
        sent = s["messages_sent"]
        got = s["laughs_received"]
        chars = s["characters_sent"]
        out.append(
            {
                "person": p,
                "messages_sent": sent,
                "characters_sent": chars,
                "laughs_given": s["laughs_given"],
                "laughs_received": got,
                "laughs_per_message": round(got / sent, 4) if sent else 0.0,
                "laughs_per_1k_chars": round(1000.0 * got / chars, 4) if chars else 0.0,
            }
        )
    return out


def finalize_reaction_people(stats: dict[str, dict], people_order: list[str]) -> list[dict]:
    out = []
    for p in people_order:
        s = stats[p]
        sent = s["messages_sent"]
        chars = s["characters_sent"]
        received = s["reactions_received"]
        out.append(
            {
                "person": p,
                "messages_sent": sent,
                "characters_sent": chars,
                "reactions_given": s["reactions_given"],
                "reactions_received": received,
                "reactions_per_message": round(received / sent, 4) if sent else 0.0,
                "reactions_per_1k_chars": round(1000.0 * received / chars, 4) if chars else 0.0,
            }
        )
    return out


def build_clip_block(people_order: list[str], clips_sent: dict, laughs_on: dict) -> dict:
    rows = []
    for p in people_order:
        sent = clips_sent.get(p, 0)
        got = laughs_on.get(p, 0)
        rows.append(
            {
                "person": p,
                "clips_sent": sent,
                "laughs_on_clips": got,
                "laughs_per_clip": round(got / sent, 3) if sent else 0.0,
            }
        )
    return {
        "total_clips": sum(r["clips_sent"] for r in rows),
        "total_laughs": sum(r["laughs_on_clips"] for r in rows),
        "by_laughs": sorted(rows, key=lambda r: (-r["laughs_on_clips"], -r["clips_sent"], r["person"])),
        "by_drops": sorted(rows, key=lambda r: (-r["clips_sent"], -r["laughs_on_clips"], r["person"])),
        "by_rate": sorted(
            rows,
            key=lambda r: (-r["laughs_per_clip"], -r["laughs_on_clips"], -r["clips_sent"], r["person"]),
        ),
    }


def analyze(conn: sqlite3.Connection, chat_ids: list[int], chat_name: str, names: dict[str, str]) -> dict:
    rows = fetch_messages(conn, chat_ids)
    # Deduplicate the same message appearing in multiple threads
    seen_rowids: set[int] = set()
    uniq: list[sqlite3.Row] = []
    for r in rows:
        if r["rowid"] in seen_rowids:
            continue
        seen_rowids.add(r["rowid"])
        uniq.append(r)

    by_guid: dict[str, sqlite3.Row] = {}
    normal: list[tuple[sqlite3.Row, str, datetime | None]] = []
    reactions: dict[str, list[tuple[sqlite3.Row, str, datetime | None, sqlite3.Row | None, str | None]]] = {
        kind: [] for kind in REACTION_TYPES
    }

    for r in uniq:
        person = display_person(r["handle"], bool(r["is_from_me"]), names)
        dt = apple_to_dt(r["date"])
        if is_normal_message(r):
            if r["guid"]:
                by_guid[r["guid"]] = r
            normal.append((r, person, dt))

    for r in uniq:
        if r["associated_message_type"] is None:
            continue
        kind = REACTION_KIND_BY_TYPE.get(int(r["associated_message_type"]))
        if kind is None:
            continue
        reactor = display_person(r["handle"], bool(r["is_from_me"]), names)
        dt = apple_to_dt(r["date"])
        target_guid = normalize_guid(r["associated_message_guid"])
        target = by_guid.get(target_guid) if target_guid else None
        if target is None and r["associated_message_guid"]:
            ag = r["associated_message_guid"]
            for g, msg in by_guid.items():
                if g and g in ag:
                    target = msg
                    break
        receiver = None
        if target is not None:
            receiver = display_person(target["handle"], bool(target["is_from_me"]), names)
        reactions[kind].append((r, reactor, dt, target, receiver))

    laughs = reactions["haha"]

    people_set: set[str] = set()
    for _, person, _ in normal:
        people_set.add(person)
    for items in reactions.values():
        for _, reactor, _, _, receiver in items:
            people_set.add(reactor)
            if receiver:
                people_set.add(receiver)
    people_order = sorted(people_set, key=lambda p: (p.lower() != "me", p.lower()))

    now = datetime.now(timezone.utc)
    windows = {
        "all_time": (None, now, "All time"),
        "last_year": (now - timedelta(days=365), now, "Last 365 days"),
        "last_30": (now - timedelta(days=30), now, "Last 30 days"),
    }

    def in_window(dt: datetime | None, start, end) -> bool:
        if dt is None:
            return False
        if start is not None and dt < start:
            return False
        return dt <= end

    window_out: dict = {}
    for wkey, (wstart, wend, wlabel) in windows.items():
        stats: dict[str, dict] = {p: person_stats_empty() for p in people_order}
        matrix = [[0 for _ in people_order] for _ in people_order]
        idx = {p: i for i, p in enumerate(people_order)}
        heat = [[0 for _ in range(24)] for _ in range(7)]  # Mon=0 .. Sun=6
        laughs_on_msg: Counter = Counter()
        reactors_on_msg: dict[int, Counter] = defaultdict(Counter)
        msg_meta: dict[int, dict] = {}

        clips_sent = {k: Counter() for k in ("tiktok", "instagram_reels", "youtube_shorts", "combined")}
        laughs_on_clips = {k: Counter() for k in ("tiktok", "instagram_reels", "youtube_shorts", "combined")}
        clip_msgs: list[dict] = []

        for r, person, dt in normal:
            if not in_window(dt, wstart, wend):
                continue
            stats[person]["messages_sent"] += 1
            text = r["text"] or ""
            stats[person]["characters_sent"] += len(text)
            msg_meta[r["rowid"]] = {
                "author": person,
                "text": text,
                "date": dt,
                "placeholder": not bool(text.strip()),
            }
            kinds = clip_kinds(text)
            if kinds:
                url = first_clip_url(text) or text.strip()[:120]
                clip_msgs.append(
                    {
                        "rowid": r["rowid"],
                        "author": person,
                        "text": url,
                        "date": dt,
                        "kinds": kinds,
                        "laughs": 0,
                    }
                )
                for k in kinds:
                    clips_sent[k][person] += 1
                clips_sent["combined"][person] += 1

        reaction_blocks = {}
        for kind, items in reactions.items():
            reaction_stats = {
                p: {
                    "messages_sent": stats[p]["messages_sent"],
                    "characters_sent": stats[p]["characters_sent"],
                    "reactions_given": 0,
                    "reactions_received": 0,
                }
                for p in people_order
            }
            reaction_matrix = [[0 for _ in people_order] for _ in people_order]
            for _, reactor, dt, target, receiver in items:
                if not in_window(dt, wstart, wend):
                    continue
                reaction_stats[reactor]["reactions_given"] += 1
                if target is None or receiver is None:
                    continue
                reaction_stats[receiver]["reactions_received"] += 1
                reaction_matrix[idx[reactor]][idx[receiver]] += 1
            reaction_people = finalize_reaction_people(reaction_stats, people_order)
            reaction_blocks[kind] = {
                "total_reactions": sum(row["reactions_given"] for row in reaction_people),
                "people": reaction_people,
                "matrix": reaction_matrix,
            }

        for r, reactor, dt, target, receiver in laughs:
            if not in_window(dt, wstart, wend):
                continue
            stats[reactor]["laughs_given"] += 1
            if dt is not None:
                local = dt.astimezone(TZ)
                # Monday=0
                heat[local.weekday()][local.hour] += 1
            if target is None or receiver is None:
                continue
            stats[receiver]["laughs_received"] += 1
            matrix[idx[reactor]][idx[receiver]] += 1
            laughs_on_msg[target["rowid"]] += 1
            reactors_on_msg[target["rowid"]][reactor] += 1

        # Clip laughs: Hahas on messages that were clip drops
        clip_rowids = {c["rowid"] for c in clip_msgs}
        for rowid, n in laughs_on_msg.items():
            if rowid not in clip_rowids:
                continue
            meta = msg_meta.get(rowid)
            if not meta:
                continue
            kinds = clip_kinds(meta["text"])
            author = meta["author"]
            for k in kinds:
                laughs_on_clips[k][author] += n
            if kinds:
                laughs_on_clips["combined"][author] += n
            for c in clip_msgs:
                if c["rowid"] == rowid:
                    c["laughs"] = n

        # Funniest
        funniest = []
        ranked_msgs = sorted(laughs_on_msg.items(), key=lambda kv: (-kv[1], kv[0]))[:30]
        # Build chronological normal list for context
        chron_normals = [(r, person, dt) for r, person, dt in normal if in_window(dt, wstart, wend)]
        rowid_to_pos = {r["rowid"]: i for i, (r, _, _) in enumerate(chron_normals)}

        for rowid, nlaughs in ranked_msgs:
            meta = msg_meta.get(rowid)
            if not meta:
                continue
            text = (meta["text"] or "").strip()
            placeholder = not bool(text)
            if placeholder:
                text = "[attachment / sticker / photo]"
            reactors = [
                {"person": p, "n": c}
                for p, c in sorted(reactors_on_msg[rowid].items(), key=lambda kv: (-kv[1], kv[0]))
            ]
            context = []
            pos = rowid_to_pos.get(rowid)
            if pos is not None:
                start = max(0, pos - 7)
                for j in range(start, pos + 1):
                    rr, auth, ddt = chron_normals[j]
                    t = (rr["text"] or "").strip()
                    ph = not bool(t)
                    context.append(
                        {
                            "author": auth,
                            "text": t if t else "[attachment / sticker / photo]",
                            "date": ddt.isoformat() if ddt else None,
                            "is_punchline": j == pos,
                            "placeholder": ph,
                        }
                    )
            funniest.append(
                {
                    "id": hashlib.sha1(str(rowid).encode()).hexdigest()[:10],
                    "laughs": nlaughs,
                    "author": meta["author"],
                    "text": text,
                    "placeholder": placeholder,
                    "date": meta["date"].isoformat() if meta["date"] else None,
                    "reactors": reactors,
                    "media": [],
                    "context": context,
                    "char_len": len(text),
                }
            )

        top_clips = sorted(
            [c for c in clip_msgs if c["laughs"] > 0],
            key=lambda c: (-c["laughs"], c["author"]),
        )[:20]
        top_clips_out = [
            {
                "author": c["author"],
                "laughs": c["laughs"],
                "kinds": c["kinds"],
                "text": c["text"],
                "date": c["date"].isoformat() if c["date"] else None,
            }
            for c in top_clips
        ]

        dated = [dt for _, _, dt in normal if in_window(dt, wstart, wend) and dt]
        dated += [
            dt
            for items in reactions.values()
            for _, _, dt, _, _ in items
            if in_window(dt, wstart, wend) and dt
        ]
        w_from = min(dated).isoformat() if dated else None
        w_to = max(dated).isoformat() if dated else None

        people_rows = finalize_people(stats, people_order)
        window_out[wkey] = {
            "label": wlabel,
            "from": w_from,
            "to": w_to,
            "total_messages": sum(s["messages_sent"] for s in stats.values()),
            "total_laughs": sum(s["laughs_given"] for s in stats.values()),
            "people": people_rows,
            "matrix": matrix,
            "reactions": reaction_blocks,
            "heatmap": heat,
            "funniest": funniest,
            "clips": {
                "tiktok": build_clip_block(people_order, clips_sent["tiktok"], laughs_on_clips["tiktok"]),
                "instagram_reels": build_clip_block(
                    people_order, clips_sent["instagram_reels"], laughs_on_clips["instagram_reels"]
                ),
                "youtube_shorts": build_clip_block(
                    people_order, clips_sent["youtube_shorts"], laughs_on_clips["youtube_shorts"]
                ),
                "combined": build_clip_block(
                    people_order, clips_sent["combined"], laughs_on_clips["combined"]
                ),
                "top_clips": top_clips_out,
            },
        }

    # Monthly race (cumulative)
    month_keys: list[str] = []
    month_set: set[str] = set()
    for _, _, dt in normal:
        if dt:
            month_set.add(dt.strftime("%Y-%m"))
    for _, _, dt, _, _ in laughs:
        if dt:
            month_set.add(dt.strftime("%Y-%m"))
    month_keys = sorted(month_set)

    cum_recv = {p: 0 for p in people_order}
    cum_given = {p: 0 for p in people_order}
    cum_msgs = {p: 0 for p in people_order}
    cum_chars = {p: 0 for p in people_order}

    race_received = []
    race_given = []
    race_per_msg = []
    race_per_1k = []

    normals_by_month: dict[str, list] = defaultdict(list)
    laughs_by_month: dict[str, list] = defaultdict(list)
    for item in normal:
        if item[2]:
            normals_by_month[item[2].strftime("%Y-%m")].append(item)
    for item in laughs:
        if item[2]:
            laughs_by_month[item[2].strftime("%Y-%m")].append(item)

    for mk in month_keys:
        for r, person, dt in normals_by_month.get(mk, []):
            cum_msgs[person] += 1
            cum_chars[person] += len(r["text"] or "")
        for r, reactor, dt, target, receiver in laughs_by_month.get(mk, []):
            cum_given[reactor] += 1
            if receiver:
                cum_recv[receiver] += 1
        race_received.append({"month": mk, "values": dict(cum_recv)})
        race_given.append({"month": mk, "values": dict(cum_given)})
        race_per_msg.append(
            {
                "month": mk,
                "values": {
                    p: round(cum_recv[p] / cum_msgs[p], 4) if cum_msgs[p] else 0.0 for p in people_order
                },
            }
        )
        race_per_1k.append(
            {
                "month": mk,
                "values": {
                    p: round(1000.0 * cum_recv[p] / cum_chars[p], 4) if cum_chars[p] else 0.0
                    for p in people_order
                },
            }
        )

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "chat_name": chat_name,
        "chats_included": len(chat_ids),
        "people": people_order,
        "timezone": "EST",
        "timezone_note": "America/New_York (Eastern — EST/EDT)",
        "window": window_out,
        "race": {
            "months": month_keys,
            "received": race_received,
            "given": race_given,
            "per_message": race_per_msg,
            "per_1k_chars": race_per_1k,
        },
    }


def write_csv_summary(data: dict, path: Path) -> None:
    people = data["window"]["all_time"]["people"]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "person",
                "messages_sent",
                "characters_sent",
                "laughs_given",
                "laughs_received",
                "laughs_per_message",
                "laughs_per_1k_chars",
            ],
        )
        w.writeheader()
        for row in sorted(people, key=lambda r: (-r["laughs_received"], r["person"])):
            w.writerow(row)


def prompt(msg: str) -> str:
    try:
        return input(msg).strip()
    except EOFError:
        return ""


def interactive_pick(conn: sqlite3.Connection, include_individual: bool = False, contact_names: dict[str, str] | None = None) -> tuple[list[int], str]:
    chat_type = "chat (group or individual)" if include_individual else "group chat name"
    print(f"\nWhat is the {chat_type}? (part of the name is fine)")
    q = prompt("> ")
    if not q:
        die("No chat name entered.")
    min_participants = 1 if include_individual else 2
    chats = list_chats(conn, limit=200, min_participants=min_participants)
    contacts = contact_names or {}
    ql = q.lower()
    matches = []
    for c in chats:
        display = (c["display_name"] or "").lower()
        ident = (c["chat_identifier"] or "").lower()
        resolved = (resolve_chat_contact(c, contacts) or "").lower()
        if ql in display or ql in ident or (resolved and ql in resolved):
            matches.append(c)
    if not matches:
        chat_label = "chats" if include_individual else "group chats"
        print(f"\nNo matches. Biggest {chat_label} on this Mac:\n")
        for i, c in enumerate(chats[:15], 1):
            resolved = resolve_chat_contact(c, contacts)
            label = resolved or c["display_name"] or ""
            print(
                f"  {i:>2}. id={c['chat_id']:<5} members~{c['participants']:<3} "
                f"msgs~{c['message_count']:<7} {label}"
            )
        die("Try again with a name from the list above.")

    print(f"\nFound {len(matches)} chat thread(s) matching {q!r}:\n")
    for i, c in enumerate(matches[:30], 1):
        resolved = resolve_chat_contact(c, contacts)
        label = resolved or c["display_name"] or c["chat_identifier"] or ""
        if resolved and c["display_name"] and resolved.lower() != c["display_name"].lower():
            label = f"{resolved} ({c['display_name']})"
        print(
            f"  {i:>2}. id={c['chat_id']:<5} members~{c['participants']:<3} "
            f"msgs~{c['message_count']:<7} {label}"
        )

    if len(matches) == 1:
        chosen = matches
        print("\nUsing that one thread.")
    else:
        print(
            "\nType ALL to combine every match (recommended if the group was renamed),\n"
            "or type a number to use one thread."
        )
        ans = prompt("> ").lower()
        if ans in ("", "all", "a"):
            chosen = matches
        else:
            try:
                n = int(ans)
                chosen = [matches[n - 1]]
            except (ValueError, IndexError):
                die("Could not understand that choice.")

    chat_ids = [c["chat_id"] for c in chosen]
    chat_name = resolve_chat_contact(chosen[0], contacts) or chosen[0]["display_name"] or q
    return chat_ids, chat_name


def main() -> None:
    parser = argparse.ArgumentParser(description="Export iMessage laugh stats for Laugh Board")
    parser.add_argument("--chat", help="Group chat name substring")
    parser.add_argument("--chat-id", type=int, help="Single chat id")
    parser.add_argument("--all-matching", action="store_true", help="Combine all chats matching --chat")
    parser.add_argument("--names", type=Path, help="Optional JSON map of phone/email → name")
    parser.add_argument("--db", type=Path, default=MESSAGES_DB)
    parser.add_argument("--copy-to", type=Path, default=DEFAULT_COPY)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path.home() / "Desktop" / "laugh-data.json",
        help="Output JSON path (default: ~/Desktop/laugh-data.json)",
    )
    parser.add_argument(
        "--csv",
        type=Path,
        default=Path.home() / "Desktop" / "laugh-leaderboard.csv",
        help="Also write a simple CSV summary",
    )
    parser.add_argument("--list", action="store_true", help="List group chats and exit")
    parser.add_argument("--include-individual", action="store_true", 
                        help="Include 1-on-1 chats in addition to group chats")
    parser.add_argument("--auto-contacts", action="store_true",
                        help="Auto-pull names from macOS Contacts app (needs Full Disk Access)")
    args = parser.parse_args()

    print(f"Copying Messages DB → {args.copy_to}")
    db_path = copy_db(args.db, args.copy_to)
    conn = connect(db_path)

    names = load_names(args.names)
    contact_names: dict[str, str] = {}
    if args.auto_contacts:
        print("Reading contacts from AddressBook…")
        contact_names = load_contacts_from_addressbook()
        if contact_names:
            print(f"  Found {len(contact_names)} contact entries.")
        else:
            print("  No contacts found (check Full Disk Access for Terminal).")
        # Manual names override auto-contacts
        names = {**contact_names, **names}

    if args.list:
        min_participants = 1 if args.include_individual else 2
        chats = list_chats(conn, min_participants=min_participants)
        chat_type = "all chats" if args.include_individual else "group chats"
        print(f"\n{chat_type.capitalize()}:")
        print(f"{'id':>6}  {'members':>7}  {'msgs':>8}  name")
        for c in chats:
            display = (c["display_name"] or "")[:60]
            resolved = resolve_chat_contact(c, contact_names)
            if resolved and not c["display_name"]:
                ident = (c.get("chat_identifier") or "")
                display = f"{resolved}  ({ident})"
            elif resolved and c["display_name"]:
                display = c["display_name"][:60]
            print(
                f"{c['chat_id']:>6}  {c['participants']:>7}  {c['message_count']:>8}  "
                f"{display}"
            )
        return

    if args.chat_id is not None:
        chat_ids = [args.chat_id]
        row = conn.execute(
            "SELECT COALESCE(NULLIF(display_name,''), chat_identifier) AS n FROM chat WHERE ROWID=?",
            (args.chat_id,),
        ).fetchone()
        chat_name = row["n"] if row else str(args.chat_id)
    elif args.chat:
        min_participants = 1 if args.include_individual else 2
        chats = list_chats(conn, limit=300, min_participants=min_participants)
        ql = args.chat.lower()
        matches = []
        for c in chats:
            display = (c["display_name"] or "").lower()
            ident = (c["chat_identifier"] or "").lower()
            resolved = (resolve_chat_contact(c, contact_names) or "").lower()
            if ql in display or ql in ident or (resolved and ql in resolved):
                matches.append(c)
        if not matches:
            die(f"No chat matched {args.chat!r}. Try --list")
        if args.all_matching or len(matches) == 1:
            chat_ids = [c["chat_id"] for c in matches]
        else:
            print("Multiple matches; re-run with --all-matching or --chat-id:", file=sys.stderr)
            for c in matches[:20]:
                resolved = resolve_chat_contact(c, contact_names)
                label = resolved or c["display_name"] or c["chat_identifier"] or ""
                print(f"  id={c['chat_id']}  {label}", file=sys.stderr)
            die("Pick one.")
        chat_name = resolve_chat_contact(matches[0], contact_names) or matches[0]["display_name"] or args.chat
    else:
        chat_ids, chat_name = interactive_pick(conn, include_individual=args.include_individual, contact_names=contact_names)

    print(f"\nAnalyzing {len(chat_ids)} thread(s) for “{chat_name}”…")
    data = analyze(conn, chat_ids, chat_name, names)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data))
    write_csv_summary(data, args.csv)

    # Also drop a copy next to this script for easy open index.html flow
    local = Path(__file__).resolve().parent / "laugh-data.json"
    local.write_text(json.dumps(data))

    w = data["window"]["all_time"]
    print(
        f"\nDone.\n"
        f"  Messages: {w['total_messages']:,}  ·  Hahas: {w['total_laughs']:,}  ·  People: {len(data['people'])}\n"
        f"  JSON → {args.out}\n"
        f"  CSV  → {args.csv}\n"
        f"  Also → {local}\n\n"
        f"Next: double-click index.html (or run: open index.html)\n"
        f"Then drop laugh-data.json onto the page."
    )


if __name__ == "__main__":
    main()
