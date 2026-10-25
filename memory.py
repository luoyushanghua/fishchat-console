# -*- coding: utf-8 -*-
"""
大肥鱼 · 对话记忆库（SQLite，零依赖）

键设计：
  g:<群号>   群聊共享记忆（群里所有人看的同一份上下文）
  u:<QQ号>   个人记忆（私聊用它；在群里说的话也会记到这里，换群/私聊仍能接上）
"""
import os
import re
import sqlite3
from contextlib import contextmanager
import threading
import time


class MemoryStore:
    def __init__(self, path, max_turns=6, ttl_days=7, max_rows=20000):
        self.path = path
        self.max_turns = max_turns
        self.ttl_days = ttl_days
        self.max_rows = max_rows
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # 长期记忆独立库：清对话缓存、重建 memory.db 都不会动它
        self.lt_path = os.path.join(os.path.dirname(path) or ".", "longterm.db")
        self._init_db()
        self._migrate_longterm()

    def _conn(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _conn_lt(self):
        c = sqlite3.connect(self.lt_path, timeout=10)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    @contextmanager
    def _db_lt(self):
        c = self._conn_lt()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _migrate_longterm(self):
        """把老 memory.db 里的事实/摘要迁到 longterm.db（只做一次）"""
        try:
            with self._db_lt() as lt:
                lt.execute("""CREATE TABLE IF NOT EXISTS facts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, chat_key TEXT NOT NULL,
                        subject TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
                        weight INTEGER NOT NULL DEFAULT 1, ts INTEGER NOT NULL)""")
                lt.execute("""CREATE TABLE IF NOT EXISTS summaries (
                        chat_key TEXT PRIMARY KEY, summary TEXT NOT NULL,
                        upto_id INTEGER NOT NULL DEFAULT 0, ts INTEGER NOT NULL)""")
                lt.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_lt_fact ON facts(chat_key, content)")
                lt.execute("""CREATE TABLE IF NOT EXISTS topics (
                        chat_key TEXT PRIMARY KEY, topic TEXT NOT NULL,
                        ts INTEGER NOT NULL)""")
            src = sqlite3.connect(self.path, timeout=10)
            try:
                rows = src.execute("SELECT chat_key,subject,content,weight,ts FROM facts").fetchall()
                sums = src.execute("SELECT chat_key,summary,upto_id,ts FROM summaries").fetchall()
            except Exception:
                rows, sums = [], []
            finally:
                src.close()
            if rows or sums:
                with self._db_lt() as lt:
                    for k, s, c, w, t in rows:
                        try:
                            lt.execute("INSERT OR IGNORE INTO facts(chat_key,subject,content,weight,ts) "
                                       "VALUES(?,?,?,?,?)", (k, s, c, w, t))
                        except Exception:
                            pass
                    for k, sm, up, t in sums:
                        try:
                            lt.execute("INSERT OR REPLACE INTO summaries(chat_key,summary,upto_id,ts) "
                                       "VALUES(?,?,?,?)", (k, sm, up, t))
                        except Exception:
                            pass
        except Exception:
            pass


    @contextmanager
    def _db(self):
        """每次操作开新连接，结束一定关闭（避免连接泄漏与旧快照）"""
        c = self._conn()
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init_db(self):
        with self._lock:
            with self._db() as c:
                c.execute("""
                    CREATE TABLE IF NOT EXISTS messages (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_key TEXT    NOT NULL,
                        role     TEXT    NOT NULL,
                        content  TEXT    NOT NULL,
                        ts       INTEGER NOT NULL
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_key_id ON messages(chat_key, id DESC)")
                c.execute("CREATE INDEX IF NOT EXISTS idx_ts ON messages(ts)")

                # 长期记忆：事实
                c.execute("""
                    CREATE TABLE IF NOT EXISTS facts (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        chat_key TEXT    NOT NULL,
                        subject  TEXT    NOT NULL DEFAULT '',
                        content  TEXT    NOT NULL,
                        weight   INTEGER NOT NULL DEFAULT 1,
                        ts       INTEGER NOT NULL
                    )
                """)
                c.execute("CREATE INDEX IF NOT EXISTS idx_fact_key ON facts(chat_key, weight DESC)")
                c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_uniq ON facts(chat_key, content)")

                # 长期记忆：滚动摘要
                c.execute("""
                    CREATE TABLE IF NOT EXISTS summaries (
                        chat_key TEXT PRIMARY KEY,
                        summary  TEXT NOT NULL,
                        upto_id  INTEGER NOT NULL DEFAULT 0,
                        ts       INTEGER NOT NULL
                    )
                """)

    # ---------- 长期记忆：事实 ----------
    def add_fact(self, chat_key, content, subject="", weight=1):
        content = (content or "").strip()
        if not chat_key or not content:
            return False
        with self._lock:
            with self._db_lt() as c:
                try:
                    c.execute(
                        "INSERT INTO facts(chat_key, subject, content, weight, ts) VALUES(?,?,?,?,?)",
                        (chat_key, subject, content, int(weight), int(time.time())))
                    return True
                except sqlite3.IntegrityError:
                    c.execute("UPDATE facts SET weight=weight+1, ts=? WHERE chat_key=? AND content=?",
                              (int(time.time()), chat_key, content))
                    return False

    def get_facts(self, chat_key, limit=20):
        with self._lock:
            with self._db_lt() as c:
                rows = c.execute(
                    "SELECT subject, content, weight FROM facts WHERE chat_key=? "
                    "ORDER BY weight DESC, ts DESC LIMIT ?", (chat_key, limit)).fetchall()
        return [{"subject": s, "content": t, "weight": w} for s, t, w in rows]

    def search_facts(self, chat_key, query, limit=8):
        """按字词重合度挑相关事实（无需向量模型）"""
        facts = self.get_facts(chat_key, 200)
        if not facts:
            return []
        q = set(re.findall(r"[\u4e00-\u9fa5]{2}|[a-zA-Z]{2,}", query or ""))
        if not q:
            return facts[:limit]
        scored = []
        for f in facts:
            txt = f["subject"] + f["content"]
            hit = sum(1 for t in q if t in txt)
            if hit:
                scored.append((hit + f["weight"] * 0.5, f))
        scored.sort(key=lambda x: -x[0])
        picked = [f for _, f in scored[:limit]]
        return picked or facts[:3]

    def clear_facts(self, chat_key=None):
        with self._lock:
            with self._db_lt() as c:
                if chat_key:
                    cur = c.execute("DELETE FROM facts WHERE chat_key=?", (chat_key,))
                else:
                    cur = c.execute("DELETE FROM facts")
                return cur.rowcount or 0

    # ---------- 长期记忆：摘要 ----------
    def get_summary(self, chat_key):
        with self._lock:
            with self._db_lt() as c:
                r = c.execute("SELECT summary, upto_id FROM summaries WHERE chat_key=?",
                              (chat_key,)).fetchone()
        return {"summary": r[0], "upto_id": r[1]} if r else {"summary": "", "upto_id": 0}

    def set_summary(self, chat_key, summary, upto_id=0):
        with self._lock:
            with self._db_lt() as c:
                c.execute(
                    "INSERT INTO summaries(chat_key, summary, upto_id, ts) VALUES(?,?,?,?) "
                    "ON CONFLICT(chat_key) DO UPDATE SET summary=excluded.summary, "
                    "upto_id=excluded.upto_id, ts=excluded.ts",
                    (chat_key, summary, int(upto_id), int(time.time())))

    # ---------- 话题追踪 ----------
    def set_topic(self, chat_key, topic, ts=None):
        topic = (topic or "").strip()[:200]
        if not chat_key or not topic:
            return False
        try:
            with self._db_lt() as c:
                c.execute("INSERT INTO topics(chat_key, topic, ts) VALUES(?,?,?) "
                          "ON CONFLICT(chat_key) DO UPDATE SET topic=excluded.topic, ts=excluded.ts",
                          (chat_key, topic, int(ts or time.time())))
            return True
        except Exception:
            return False

    def get_topic(self, chat_key, max_age=1800):
        try:
            with self._db_lt() as c:
                r = c.execute("SELECT topic, ts FROM topics WHERE chat_key=?",
                              (chat_key,)).fetchone()
            if not r:
                return None
            topic, ts = r
            if time.time() - ts > max_age:
                return None
            return {"topic": topic, "ts": ts, "age": int(time.time() - ts)}
        except Exception:
            return None

    # ---------- 写入 ----------
    def add(self, chat_key, role, content):
        if not chat_key or not content:
            return
        with self._lock:
            with self._db() as c:
                c.execute(
                    "INSERT INTO messages(chat_key, role, content, ts) VALUES(?,?,?,?)",
                    (chat_key, role, content, int(time.time())),
                )

    # ---------- 长期记忆：导出 / 打包 ----------
    def export_longterm(self, out_dir=None):
        """把长期记忆按会话分别打包成 JSON，永久留存"""
        import json as _json
        out_dir = out_dir or os.path.join(os.path.dirname(self.path) or ".", "longterm_pack")
        os.makedirs(out_dir, exist_ok=True)
        with self._db_lt() as c:
            facts = c.execute("SELECT chat_key,subject,content,weight,ts FROM facts").fetchall()
            sums = c.execute("SELECT chat_key,summary,upto_id,ts FROM summaries").fetchall()
        packs = {}
        for k, sub, con, w, t in facts:
            packs.setdefault(k, {"chat_key": k, "summary": "", "facts": []})["facts"].append(
                {"subject": sub, "content": con, "weight": w, "ts": t})
        for k, sm, up, t in sums:
            d = packs.setdefault(k, {"chat_key": k, "summary": "", "facts": []})
            d["summary"] = sm
            d["summary_ts"] = t
        stamp = time.strftime("%Y%m%d_%H%M%S")
        names = []
        for k, data in packs.items():
            fn = (k.replace(":", "_").replace("/", "_") or "all") + ".json"
            data["exported_at"] = stamp
            with open(os.path.join(out_dir, fn), "w", encoding="utf-8", newline="\n") as f:
                _json.dump(data, f, ensure_ascii=False, indent=2)
            names.append(fn)
        return {"ok": True, "dir": out_dir, "files": len(names),
                "names": names[:30], "facts": len(facts), "summaries": len(sums)}

    def longterm_stats(self):
        try:
            with self._db_lt() as c:
                f = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                s = c.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
            size = os.path.getsize(self.lt_path) if os.path.isfile(self.lt_path) else 0
            return {"facts": f, "summaries": s, "size_kb": round(size / 1024, 1),
                    "path": self.lt_path}
        except Exception:
            return {"facts": 0, "summaries": 0, "size_kb": 0, "path": self.lt_path}

    # ---------- 读取 ----------
    def history(self, chat_key, limit=None):
        n = (limit or self.max_turns) * 2
        with self._lock:
            with self._db() as c:
                rows = c.execute(
                    "SELECT role, content FROM messages WHERE chat_key=? ORDER BY id DESC LIMIT ?",
                    (chat_key, n),
                ).fetchall()
        rows.reverse()
        return [{"role": r, "content": t} for r, t in rows]

    # ---------- 清理 ----------
    def cleanup(self):
        """删除过期消息 + 每个键只保留最近 N 轮 + 总量封顶"""
        now = int(time.time())
        cutoff = now - self.ttl_days * 86400
        keep_rows = self.max_turns * 2
        stats = {"expired": 0, "trimmed": 0, "capped": 0}
        with self._lock:
            with self._db() as c:
                cur = c.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
                stats["expired"] = cur.rowcount or 0

                cur = c.execute("""
                    DELETE FROM messages WHERE id NOT IN (
                        SELECT id FROM (
                            SELECT id, ROW_NUMBER() OVER (
                                PARTITION BY chat_key ORDER BY id DESC
                            ) AS rn FROM messages
                        ) WHERE rn <= ?
                    )
                """, (keep_rows,))
                stats["trimmed"] = cur.rowcount or 0

                total = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                if total > self.max_rows:
                    excess = total - self.max_rows
                    cur = c.execute(
                        "DELETE FROM messages WHERE id IN "
                        "(SELECT id FROM messages ORDER BY id ASC LIMIT ?)", (excess,))
                    stats["capped"] = cur.rowcount or 0

                try:
                    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
                try:
                    c.execute("VACUUM")
                except Exception:
                    pass
        stats["remaining"] = self.stats()["rows"]
        return stats

    # ---------- 统计 ----------
    def stats(self):
        try:
            with self._lock:
                with self._db() as c:
                    rows = c.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
                    keys = c.execute("SELECT COUNT(DISTINCT chat_key) FROM messages").fetchone()[0]
                    last = c.execute("SELECT MAX(ts) FROM messages").fetchone()[0]
                    try:
                        facts_n = c.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
                        summ_n = c.execute("SELECT COUNT(*) FROM summaries").fetchone()[0]
                    except Exception:
                        facts_n = summ_n = 0
            size = os.path.getsize(self.path) if os.path.isfile(self.path) else 0
            wal = self.path + "-wal"
            if os.path.isfile(wal):
                size += os.path.getsize(wal)
            return {"rows": rows, "keys": keys, "facts": facts_n, "summaries": summ_n,
                    "size_kb": round(size / 1024, 1), "last_ts": last or 0}
        except Exception as e:
            return {"rows": 0, "keys": 0, "size_kb": 0, "last_ts": 0, "error": str(e)}

    def clear(self, chat_key=None):
        with self._lock:
            with self._db() as c:
                if chat_key:
                    cur = c.execute("DELETE FROM messages WHERE chat_key=?", (chat_key,))
                else:
                    cur = c.execute("DELETE FROM messages")
                n = cur.rowcount or 0
                try:
                    c.execute("VACUUM")
                except Exception:
                    pass
        return n

    def list_keys(self, limit=50):
        with self._lock:
            with self._db() as c:
                rows = c.execute(
                    "SELECT chat_key, COUNT(*) n, MAX(ts) ts FROM messages "
                    "GROUP BY chat_key ORDER BY ts DESC LIMIT ?", (limit,)
                ).fetchall()
        return [{"key": k, "count": n, "last_ts": t} for k, n, t in rows]
