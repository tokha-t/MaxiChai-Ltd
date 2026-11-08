"""SQLite-хранилище: диалоги, версии чек-листов, оценки, апелляции. Без ORM, только stdlib."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .checklist import ensure_defaults, load_default, validate_checklist
from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY,
  group_id TEXT,
  channel TEXT,
  language TEXT,
  data_json TEXT NOT NULL,
  imported_at TEXT
);
CREATE TABLE IF NOT EXISTS dataset_manifests (
  group_id TEXT PRIMARY KEY,
  manifest_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checklists (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  version INTEGER,
  data_json TEXT NOT NULL,
  note TEXT,
  created_at TEXT,
  is_active INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id TEXT,
  checklist_id INTEGER,
  status TEXT,
  final_score REAL,
  critical_flag INTEGER,
  needs_review INTEGER,
  model TEXT,
  cost_usd REAL,
  latency_sec REAL,
  result_json TEXT NOT NULL,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_eval_conv ON evaluations(conversation_id, id);
CREATE TABLE IF NOT EXISTS appeals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id INTEGER,
  conversation_id TEXT,
  criterion_id TEXT,
  comment TEXT,
  author TEXT,
  status TEXT DEFAULT 'open',
  original_value_json TEXT,
  decision_value_json TEXT,
  decision_reason TEXT,
  reviewer TEXT,
  ai_review_json TEXT,
  created_at TEXT,
  decided_at TEXT
);
"""

_lock = threading.RLock()
DEFAULT_NOTE = "Исходный чек-лист организаторов"


def _fingerprint(cl: Dict[str, Any]) -> str:
    from .checklist import criterion_hash
    cl = ensure_defaults(cl)
    parts = [f"{c['id']}:{criterion_hash(c, cl)}:{c.get('weight')}:{c.get('section')}" for c in cl.get("criteria", [])]
    return "|".join(parts)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    """Своё соединение SQLite на каждый поток: сервер обслуживает запросы и фоновые оценки параллельно."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path or settings.db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with _lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        active = self.active_checklist()
        if active is None:
            self.save_checklist(load_default(), note=DEFAULT_NOTE, activate=True)
        elif (active.get("_note") or "").startswith(DEFAULT_NOTE) and _fingerprint(active) != _fingerprint(load_default()):
            # активен нетронутый исходный чек-лист, а в config/ подсказки уточнены — подхватываем новой версией
            self.save_checklist(load_default(), note=DEFAULT_NOTE + " (уточнены подсказки для ИИ)", activate=True)

    @property
    def conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    # ---------- диалоги ----------
    def get_dataset_manifest(self, group_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT manifest_json FROM dataset_manifests WHERE group_id=?", (group_id,)).fetchone()
        return json.loads(row["manifest_json"]) if row else None

    def register_dataset_manifest(self, records: List[Dict[str, Any]], group_id: str,
                                  source: str = "organizer") -> Dict[str, Any]:
        """Неизменяемый состав исходного импорта; ручная загрузка диалогов его не создаёт."""
        from .dataset import normalize_record
        from .evaluator import input_hash
        if not group_id or source not in ("organizer", "cache", "file"):
            raise ValueError("Для манифеста нужны group_id и источник исходного набора")
        records = [normalize_record(r) for r in records]
        ids = [r["conversation_id"] for r in records]
        if not ids or any(not cid for cid in ids) or len(set(ids)) != len(ids):
            raise ValueError("В исходном наборе нужны непустые уникальные conversation_id")
        hashes = {r["conversation_id"]: input_hash(r) for r in records}
        counts = {r["conversation_id"]: len(r["transcript"]) for r in records}
        digest = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode("utf-8")).hexdigest()
        manifest = {"group_id": group_id, "source": source, "conversation_ids": ids,
                    "input_hashes": hashes, "turn_counts": counts, "manifest_hash": digest, "created_at": _now()}
        with _lock:
            previous = self.get_dataset_manifest(group_id)
            if previous:
                if previous["input_hashes"] != hashes or previous["turn_counts"] != counts:
                    raise ValueError(f"Состав или входные данные набора {group_id} отличаются от исходного манифеста")
                return previous
            self.conn.execute("INSERT INTO dataset_manifests(group_id, manifest_json) VALUES(?,?)",
                              (group_id, json.dumps(manifest, ensure_ascii=False)))
            self.conn.commit()
        return manifest

    def upsert_conversations(self, records: List[Dict[str, Any]], group_id: str) -> int:
        with _lock:
            for r in records:
                row = self.conn.execute("SELECT data_json FROM conversations WHERE id=?", (r["conversation_id"],)).fetchone()
                if row:
                    old = json.loads(row["data_json"])
                    if (old.get("transcript"), old.get("channel")) != (r.get("transcript"), r.get("channel")):
                        # тот же ID, другое содержание: прошлые оценки больше не относятся к этому входу
                        self.conn.execute("UPDATE evaluations SET status='stale' WHERE conversation_id=? AND status='ok'",
                                          (r["conversation_id"],))
                self.conn.execute(
                    "INSERT INTO conversations(id, group_id, channel, language, data_json, imported_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET group_id=excluded.group_id, channel=excluded.channel, "
                    "language=excluded.language, data_json=excluded.data_json, imported_at=excluded.imported_at",
                    (r["conversation_id"], group_id, r.get("channel"), r.get("language"),
                     json.dumps(r, ensure_ascii=False), _now()),
                )
            self.conn.commit()
        return len(records)

    def get_conversation(self, cid: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        conv = json.loads(row["data_json"])
        conv["group_id"] = row["group_id"]
        return conv

    def list_conversations(self, group_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT id, group_id, channel, language, data_json, imported_at FROM conversations"
        args: List[Any] = []
        if group_id:
            q += " WHERE group_id=?"
            args.append(group_id)
        q += " ORDER BY imported_at, rowid"
        out = []
        for row in self.conn.execute(q, args).fetchall():
            data = json.loads(row["data_json"])
            out.append({"conversation_id": row["id"], "group_id": row["group_id"], "channel": row["channel"],
                        "language": row["language"], "turns": len(data.get("transcript") or []),
                        "audio_url": data.get("audio_url")})
        return out

    def groups(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT group_id, COUNT(*) AS n FROM conversations GROUP BY group_id").fetchall()
        return [{"group_id": r["group_id"], "count": r["n"]} for r in rows]

    # ---------- чек-листы ----------
    def save_checklist(self, data: Dict[str, Any], note: str = "", activate: bool = True) -> Dict[str, Any]:
        data = ensure_defaults(data)
        data.pop("_id", None)
        errors = validate_checklist(data)
        if errors:
            raise ValueError("; ".join(errors))
        with _lock:
            row = self.conn.execute("SELECT MAX(version) AS v FROM checklists").fetchone()
            version = int(row["v"] or 0) + 1
            data["version"] = version
            cur = self.conn.execute(
                "INSERT INTO checklists(name, version, data_json, note, created_at, is_active) VALUES(?,?,?,?,?,0)",
                (data.get("name"), version, json.dumps(data, ensure_ascii=False), note, _now()),
            )
            cl_id = cur.lastrowid
            if activate:
                self.conn.execute("UPDATE checklists SET is_active = CASE WHEN id=? THEN 1 ELSE 0 END", (cl_id,))
            self.conn.commit()
        return self.get_checklist(cl_id)

    def activate_checklist(self, cl_id: int) -> Dict[str, Any]:
        with _lock:
            self.conn.execute("UPDATE checklists SET is_active = CASE WHEN id=? THEN 1 ELSE 0 END", (cl_id,))
            self.conn.commit()
        return self.get_checklist(cl_id)

    def get_checklist(self, cl_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM checklists WHERE id=?", (cl_id,)).fetchone()
        if not row:
            return None
        data = json.loads(row["data_json"])
        data["_id"] = row["id"]
        data["_note"] = row["note"]
        data["_created_at"] = row["created_at"]
        data["_active"] = bool(row["is_active"])
        return data

    def active_checklist(self) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT id FROM checklists WHERE is_active=1 ORDER BY id DESC LIMIT 1").fetchone()
        return self.get_checklist(row["id"]) if row else None

    def list_checklists(self) -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT id, name, version, note, created_at, is_active FROM checklists ORDER BY id DESC").fetchall()
        return [{"id": r["id"], "name": r["name"], "version": r["version"], "note": r["note"],
                 "created_at": r["created_at"], "active": bool(r["is_active"])} for r in rows]

    # ---------- оценки ----------
    def save_evaluation(self, result: Dict[str, Any], checklist_id: Optional[int]) -> int:
        score = result.get("score") or {}
        needs = any(r.get("needs_review") for r in result.get("criteria") or [])
        usage = result.get("usage") or {}
        with _lock:
            cur = self.conn.execute(
                "INSERT INTO evaluations(conversation_id, checklist_id, status, final_score, critical_flag, needs_review, model, "
                "cost_usd, latency_sec, result_json, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (result["conversation_id"], checklist_id, result.get("status"), score.get("final_score"),
                 int(bool(score.get("critical_flag"))), int(needs), result.get("model"), usage.get("cost_usd"),
                 usage.get("latency_sec"), json.dumps(result, ensure_ascii=False), _now()),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def get_evaluation(self, eval_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM evaluations WHERE id=?", (eval_id,)).fetchone()
        if not row:
            return None
        res = json.loads(row["result_json"])
        res["_id"] = row["id"]
        res["_checklist_id"] = row["checklist_id"]
        res["_stored_status"] = row["status"]
        return res

    def latest_evaluation(self, cid: str, checklist_id: Optional[int] = None, ok_only: bool = False) -> Optional[Dict[str, Any]]:
        q = "SELECT id FROM evaluations WHERE conversation_id=?"
        args: List[Any] = [cid]
        if checklist_id is not None:
            q += " AND checklist_id=?"
            args.append(checklist_id)
        if ok_only:
            q += " AND status='ok'"
        row = self.conn.execute(q + " ORDER BY id DESC LIMIT 1", args).fetchone()
        return self.get_evaluation(row["id"]) if row else None

    def evaluation_history(self, cid: str) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT e.id, e.checklist_id, e.status, e.final_score, e.critical_flag, e.model, e.cost_usd, e.created_at, c.version "
            "FROM evaluations e LEFT JOIN checklists c ON c.id=e.checklist_id WHERE e.conversation_id=? ORDER BY e.id DESC", (cid,)).fetchall()
        return [dict(r) for r in rows]

    # ---------- апелляции ----------
    def create_appeal(self, evaluation_id: int, criterion_id: str, comment: str, author: str = "оператор") -> Dict[str, Any]:
        ev = self.get_evaluation(evaluation_id)
        if not ev:
            raise ValueError("Оценка не найдена")
        row = next((r for r in ev.get("criteria") or [] if r["criterion_id"] == criterion_id), None)
        if not row:
            raise ValueError("Критерий не найден в оценке")
        existing = self.conn.execute("SELECT id FROM appeals WHERE evaluation_id=? AND criterion_id=? AND status='open'",
                                     (evaluation_id, criterion_id)).fetchone()
        if existing:
            raise ValueError("По этому критерию уже есть открытая апелляция")
        with _lock:
            cur = self.conn.execute(
                "INSERT INTO appeals(evaluation_id, conversation_id, criterion_id, comment, author, status, original_value_json, created_at) "
                "VALUES(?,?,?,?,?, 'open', ?, ?)",
                (evaluation_id, ev["conversation_id"], criterion_id, comment, author, json.dumps(row.get("value")), _now()),
            )
            self.conn.commit()
        return self.get_appeal(int(cur.lastrowid))

    def get_appeal(self, appeal_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM appeals WHERE id=?", (appeal_id,)).fetchone()
        return self._appeal_dict(row) if row else None

    @staticmethod
    def _appeal_dict(row) -> Dict[str, Any]:
        d = dict(row)
        d["original_value"] = json.loads(d.pop("original_value_json") or "null")
        d["decision_value"] = json.loads(d.pop("decision_value_json") or "null")
        d["ai_review"] = json.loads(d.pop("ai_review_json") or "null")
        return d

    def list_appeals(self, status: Optional[str] = None, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        q = "SELECT * FROM appeals WHERE 1=1"
        args: List[Any] = []
        if status:
            q += " AND status=?"
            args.append(status)
        if conversation_id:
            q += " AND conversation_id=?"
            args.append(conversation_id)
        return [self._appeal_dict(r) for r in self.conn.execute(q + " ORDER BY id DESC", args).fetchall()]

    def set_appeal_ai_review(self, appeal_id: int, review: Dict[str, Any]) -> None:
        with _lock:
            self.conn.execute("UPDATE appeals SET ai_review_json=? WHERE id=?", (json.dumps(review, ensure_ascii=False), appeal_id))
            self.conn.commit()

    def decide_appeal(self, appeal_id: int, decision: str, value: Any, reason: str, reviewer: str = "проверяющий") -> Dict[str, Any]:
        if decision not in ("confirmed", "changed"):
            raise ValueError("decision: confirmed или changed")
        ap = self.get_appeal(appeal_id)
        if not ap:
            raise ValueError("Апелляция не найдена")
        if ap["status"] != "open":
            raise ValueError("Апелляция уже рассмотрена")
        if not (reason or "").strip():
            raise ValueError("Укажите причину решения")
        final_value = ap["original_value"] if decision == "confirmed" else value
        with _lock:
            self.conn.execute(
                "UPDATE appeals SET status=?, decision_value_json=?, decision_reason=?, reviewer=?, decided_at=? WHERE id=?",
                (decision, json.dumps(final_value), reason.strip(), reviewer, _now(), appeal_id),
            )
            self.conn.commit()
        return self.get_appeal(appeal_id)

    def overrides_for_evaluation(self, evaluation_id: int) -> Dict[str, Any]:
        rows = self.conn.execute("SELECT criterion_id, decision_value_json FROM appeals WHERE evaluation_id=? AND status='changed' ORDER BY id",
                                 (evaluation_id,)).fetchall()
        return {r["criterion_id"]: json.loads(r["decision_value_json"]) for r in rows}
