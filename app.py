"""
一个规划助手 - Flask 后端
包含 8 个 Agent：课程表Agent / 作息分析Agent / 任务拆解Agent / 排期Agent /
                 优先级Agent / 冲突检测Agent / 休息插入Agent / 汇总Agent
"""
import os
import io
import json
import sqlite3
import base64
import datetime
import re
from datetime import datetime as dt, timedelta, date

from flask import Flask, request, jsonify, send_from_directory, render_template
from flask_cors import CORS
from dotenv import load_dotenv
import requests

# ---------------- 初始化 ----------------
load_dotenv()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_CHAT_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_VL_URL = "https://api.deepseek.com/chat/completions"  # DeepSeek 多模态使用同一 endpoint
DEEPSEEK_VL_MODEL = "deepseek-chat"  # DeepSeek v2+ 支持图片

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "ddl_planner.db")
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = Flask(__name__, static_folder=STATIC_DIR, static_url_path="/static")
CORS(app)

# ---------------- 工具函数 ----------------
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS courses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        day_of_week INTEGER NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        location TEXT DEFAULT '',
        weeks TEXT DEFAULT '1-16',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS routines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        wake_up TEXT DEFAULT '07:00',
        sleep TEXT DEFAULT '23:00',
        meal_breakfast TEXT DEFAULT '07:30-08:00',
        meal_lunch TEXT DEFAULT '12:00-12:30',
        meal_dinner TEXT DEFAULT '18:00-18:30',
        peak_start TEXT DEFAULT '09:00',
        peak_end TEXT DEFAULT '11:00',
        fixed_tasks TEXT DEFAULT '[]',
        entertainment TEXT DEFAULT '20:00-21:00',
        semester_start TEXT DEFAULT '',
        current_week INTEGER DEFAULT 1
    );
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        due_date TEXT NOT NULL,
        type TEXT DEFAULT '其他',
        granularity TEXT DEFAULT '中',
        priority INTEGER DEFAULT 0,
        status TEXT DEFAULT 'pending',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS task_steps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        date TEXT,
        start_time TEXT,
        end_time TEXT,
        duration INTEGER DEFAULT 30,
        completed INTEGER DEFAULT 0,
        is_break INTEGER DEFAULT 0,
        FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
    );
    CREATE TABLE IF NOT EXISTS pomodoro_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        step_id INTEGER,
        duration INTEGER NOT NULL,
        mode TEXT DEFAULT 'task',
        completed_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    CREATE TABLE IF NOT EXISTS chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    );
    """)
    try:
        c.execute("ALTER TABLE courses ADD COLUMN weeks TEXT DEFAULT '1-16'")
    except Exception:
        pass
    try:
        c.execute("UPDATE courses SET weeks='1-16' WHERE weeks IS NULL OR weeks='1-18'")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE routines ADD COLUMN semester_start TEXT DEFAULT ''")
    except Exception:
        pass
    try:
        c.execute("ALTER TABLE routines ADD COLUMN current_week INTEGER DEFAULT 1")
    except Exception:
        pass
    c.execute("SELECT COUNT(*) FROM routines")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO routines DEFAULT VALUES")
    conn.commit()
    conn.close()


init_db()


def parse_time(t):
    if not t:
        return None
    try:
        return dt.strptime(t, "%H:%M").time()
    except Exception:
        return None


def to_min(t_str):
    if not t_str:
        return 0
    h, m = t_str.split(":")
    return int(h) * 60 + int(m)


def min_to_str(m):
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}"


def days_between(d1_str, d2_str):
    d1 = dt.strptime(d1_str, "%Y-%m-%d").date()
    d2 = dt.strptime(d2_str, "%Y-%m-%d").date()
    return (d2 - d1).days


def today_str():
    return date.today().strftime("%Y-%m-%d")


def is_week_active(weeks_str, current_week):
    """判断当前周是否在课程周次范围内
    
    支持格式：
    - "1-18"：连续周次
    - "1-14,17-18"：非连续周次（逗号分隔多个范围）
    - "5"：单周
    """
    if not weeks_str or not isinstance(current_week, int):
        return True
    try:
        parts = weeks_str.replace("，", ",").split(",")
        for part in parts:
            part = part.strip()
            if "-" in part:
                start, end = part.split("-", 1)
                start_w = int(start.strip())
                end_w = int(end.strip())
                if start_w <= current_week <= end_w:
                    return True
            else:
                single_w = int(part)
                if single_w == current_week:
                    return True
        return False
    except Exception:
        return True


def get_current_week():
    """获取当前周数：优先使用手动设置，否则根据学期开始日期计算"""
    conn = get_conn()
    row = conn.execute(
        "SELECT semester_start, current_week FROM routines ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return 1
    d = dict(row)
    manual_week = d.get("current_week")
    semester_start = d.get("semester_start")
    if manual_week and manual_week != 1:
        return manual_week
    if semester_start:
        try:
            start_date = dt.strptime(semester_start, "%Y-%m-%d").date()
            today_date = date.today()
            days_diff = (today_date - start_date).days
            if days_diff >= 0:
                computed_week = (days_diff // 7) + 1
                return computed_week
        except Exception:
            pass
    return manual_week or 1


# ============================================================
#                      8 个 Agent 类
# ============================================================

class CourseAgent:
    """课程表 Agent：解析课表，输出指定日期的空闲时段"""

    @staticmethod
    def get_courses_for_date(date_str, current_week=None):
        d = dt.strptime(date_str, "%Y-%m-%d").date()
        day_of_week = d.weekday() + 1  # 1=周一
        conn = get_conn()
        rows = conn.execute(
            "SELECT * FROM courses WHERE day_of_week=? ORDER BY start_time",
            (day_of_week,)
        ).fetchall()
        conn.close()
        courses = [dict(r) for r in rows]
        if current_week is not None:
            courses = [
                c for c in courses
                if is_week_active(c.get("weeks", "1-16"), current_week)
            ]
        return courses

    @staticmethod
    def get_free_slots(date_str, start_time="07:00", end_time="23:00", current_week=None):
        """返回该日期的空闲时段列表（避开课程）"""
        courses = CourseAgent.get_courses_for_date(date_str, current_week)
        occupied = [(to_min(c["start_time"]), to_min(c["end_time"])) for c in courses]
        occupied.sort()
        free = []
        cur = to_min(start_time)
        for s, e in occupied:
            if cur < s:
                free.append((cur, s))
            cur = max(cur, e)
        if cur < to_min(end_time):
            free.append((cur, to_min(end_time)))
        return free


class RoutineAgent:
    """作息分析 Agent：解析作息，输出可用时段和精力高峰"""

    @staticmethod
    def get_routine():
        conn = get_conn()
        row = conn.execute("SELECT * FROM routines ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        if not row:
            return {}
        d = dict(row)
        try:
            d["fixed_tasks"] = json.loads(d.get("fixed_tasks") or "[]")
        except Exception:
            d["fixed_tasks"] = []
        return d

    @staticmethod
    def available_window():
        r = RoutineAgent.get_routine()
        return (r.get("wake_up", "07:00"), r.get("sleep", "23:00"))

    @staticmethod
    def peak_window():
        r = RoutineAgent.get_routine()
        return (r.get("peak_start", "09:00"), r.get("peak_end", "11:00"))

    @staticmethod
    def fixed_slots_for_date(date_str):
        """用餐、固定事务、娱乐时段均视作占用（可做轻度任务）"""
        r = RoutineAgent.get_routine()
        slots = []
        for key in ("meal_breakfast", "meal_lunch", "meal_dinner", "entertainment"):
            v = r.get(key, "")
            if "-" in v:
                s, e = v.split("-", 1)
                slots.append((to_min(s), to_min(e)))
        for ft in r.get("fixed_tasks", []):
            if isinstance(ft, dict) and "time" in ft and "-" in ft["time"]:
                s, e = ft["time"].split("-", 1)
                slots.append((to_min(s), to_min(e)))
        slots.sort()
        return slots


class TaskBreakdownAgent:
    """任务拆解 Agent（增强版）

    对「考试/复习」类型：自动按总时长和粒度在截止日前分多天布置，
    每天包含 预习/复习/刷题/错题 等循环子步骤，便于巩固记忆。
    其他类型：仍按粒度对超阈值步骤进行切分。
    """

    REVIEW_CYCLE = [
        ("📖 知识点预习", 25),
        ("📝 核心要点复习", 30),
        ("✍️ 专项刷题", 45),
        ("🧠 错题整理 & 复盘", 25),
    ]

    @staticmethod
    def breakdown(task, steps, granularity="中"):
        """
        granularity: 粗/中/细
        返回新的 steps 列表
        """
        threshold = {"粗": 9999, "中": 60, "细": 30}.get(granularity, 60)

        if TaskBreakdownAgent._is_review_like(task):
            return TaskBreakdownAgent._review_breakdown(task, steps, threshold)
        return TaskBreakdownAgent._plain_breakdown(steps, threshold)

    @staticmethod
    def _is_review_like(task):
        t = (task.get("type") or "")
        n = (task.get("name") or "")
        return t in ("考试",) or any(k in n for k in ("复习", "考试", "备考"))

    @staticmethod
    def _plain_breakdown(steps, threshold):
        new_steps = []
        for i, s in enumerate(steps):
            dur = max(5, int(s.get("duration", 30)))
            name = s.get("name") or f"步骤{i+1}"
            if dur <= threshold:
                new_steps.append({"name": name, "duration": dur})
            else:
                n_parts = max(2, (dur + threshold - 1) // threshold)
                part_dur = dur // n_parts
                for j in range(n_parts):
                    new_steps.append({
                        "name": f"{name} ({j+1}/{n_parts})",
                        "duration": part_dur
                    })
        return new_steps

    @staticmethod
    def _review_breakdown(task, steps, threshold):
        """为复习类任务生成多天循环子步骤：

        策略：
        - 计算「预期复习轮数」：max(2, days_to_due/3)
        - 每轮 = REVIEW_CYCLE 标准模板
        - 若用户提供了步骤，则将用户步骤与模板辅助步骤交替（混合）
        - 不足一轮时按比例缩减（但至少保留 1 轮）
        """
        try:
            due = dt.strptime(task["due_date"], "%Y-%m-%d").date()
        except Exception:
            due = date.today() + timedelta(days=7)
        total_days = max(3, (due - date.today()).days + 1)

        user_named = [s for s in steps if (s.get("name") or "").strip()]
        user_plain = TaskBreakdownAgent._plain_breakdown(user_named, threshold) \
            if user_named else []

        # 决定总轮数：每天至少一轮，上限为 days
        n_cycles = max(2, min(total_days, max(3, total_days // 2)))
        plan = []
        template = TaskBreakdownAgent.REVIEW_CYCLE  # [(name, minutes)]
        if user_plain:
            # 混合：用户步骤 + 模板中的辅助复习项（刷题/错题）
            aux = [(n, d) for n, d in template if any(k in n for k in ("刷题", "错题", "复盘"))]
            for idx in range(n_cycles):
                for up in user_plain:
                    plan.append({"name": up["name"], "duration": up["duration"]})
                for n, d in aux[:2]:
                    plan.append({"name": f"🔁 {n}（第{idx+1}轮）", "duration": d})
        else:
            # 纯模板
            for idx in range(n_cycles):
                for n, d in template:
                    plan.append({"name": f"第{idx+1}轮 · {n}", "duration": d})
        return plan


class PriorityAgent:
    """优先级 Agent：评估紧急程度"""

    @staticmethod
    def score(task):
        """分数越高越紧急。优先级和类型权重 > 紧急度"""
        try:
            due = dt.strptime(task["due_date"], "%Y-%m-%d").date()
        except Exception:
            return 0
        today = date.today()
        delta = (due - today).days
        priority_bonus = int(task.get("priority") or 0) * 50
        type_bonus = {"考试": 200, "作业": 80, "项目": 50, "生活": 20, "其他": 0}.get(
            task.get("type", ""), 0)
        if delta < 0:
            return 1000 + abs(delta) * 30 + priority_bonus + type_bonus
        if delta == 0:
            return 500 + priority_bonus + type_bonus
        return priority_bonus + type_bonus + max(0, 100 - delta * 5)


class SchedulingAgent:
    """排期 Agent（增强版）

    核心改进：
    - 支持同时为多个任务统一排期（按 priority 从高到低依次）
    - 已完成的步骤保留不动；未完成步骤按新计划重排
    - 记录每天每任务的时间配额，同科目一天不超过 120 分钟
    - 逻辑类步骤与记忆类步骤穿插（按步骤名关键字启发式判断）
    - 预留缓冲时间；避开课程表和用餐/娱乐时段
    """

    def __init__(self):
        self.routine_agent = RoutineAgent()
        self.course_agent = CourseAgent()
        self._used_slots = {}     # date -> [(s_min, e_min, task_id)]
        self._per_day_task_min = {}  # (date, task_id) -> minutes

    # --------- 单任务调度（保留旧接口） ---------
    def schedule(self, task, steps, task_id=None):
        """对单个任务进行排期，忽略已排步骤"""
        tid = task_id or task.get("id") or "__single__"
        current_week = get_current_week()
        result = self.schedule_multi([(task, steps, tid)], current_week)
        return result.get(tid, [])

    # --------- 多任务整体调度 ---------
    def schedule_multi(self, task_step_tuples, current_week=1):
        """
        task_step_tuples: [(task_dict, steps_list, task_id), ...]
        task_dict 需要: due_date, type, priority(可选)
        steps_list: [{name, duration}]
        task_id: int 或 None
        current_week: 当前周数，用于考试周模式判断
        返回: {task_id: [scheduled_step_dict, ...]}
        """
        wake, sleep = self.routine_agent.available_window()
        peak_s, peak_e = self.routine_agent.peak_window()
        peak_lo = to_min(peak_s)
        peak_hi = to_min(peak_e)
        is_exam_week = current_week > 16

        scored = []
        for task, steps, task_id in task_step_tuples:
            tid = task_id or task.get("id") or f"task_{len(scored)}"
            s = PriorityAgent.score(task)
            scored.append((s, tid, task, steps))
        scored.sort(key=lambda x: -x[0])

        result_map = {}
        for _score, tid, task, steps in scored:
            scheduled = self._schedule_one(
                task, steps, tid, wake, sleep, peak_lo, peak_hi, current_week
            )
            d_list = list(self._used_slots.keys())
            for d in d_list:
                self._used_slots[d] = [(s, e, t) for s, e, t in self._used_slots[d] if t != tid]
                if not self._used_slots[d]:
                    del self._used_slots[d]
            with_break = BreakInsertAgent.insert_breaks(scheduled)
            for s in with_break:
                if s.get("is_break"):
                    continue
                d = s["date"]
                self._used_slots.setdefault(d, []).append(
                    (to_min(s["start_time"]), to_min(s["end_time"]), tid)
                )
            result_map[tid] = with_break
        return result_map

    def _schedule_one(self, task, steps, task_id, wake, sleep, peak_lo, peak_hi, current_week=1):
        if not steps:
            return []
        start = date.today()
        try:
            due = dt.strptime(task["due_date"], "%Y-%m-%d").date()
        except Exception:
            due = date.today() + timedelta(days=7)
        if due < start:
            due = start + timedelta(days=3)

        days_left = (due - start).days + 1
        total_min = sum(int(s.get("duration", 30)) for s in steps)

        is_exam_week = current_week > 16

        if is_exam_week:
            ratio = 0.50
        elif days_left <= 3:
            ratio = 0.40
        elif days_left <= 7:
            ratio = 0.30
        else:
            ratio = 0.20

        day_total = (to_min(sleep) - to_min(wake)) * ratio * 0.85
        soft_cap = max(day_total, 120)
        soft_cap = min(soft_cap, 180)

        task_type_hard = {"考试", "项目"}
        is_hard = task.get("type") in task_type_hard

        scheduled = []
        remaining = list(steps)
        day_cursor = start
        max_extra = 30
        guard = 0
        while remaining:
            if guard > days_left + max_extra:
                break
            guard += 1
            if day_cursor > due + timedelta(days=max_extra):
                break
            date_str = day_cursor.strftime("%Y-%m-%d")
            if is_exam_week:
                free_slots = [(to_min(wake), to_min(sleep))]
            else:
                free_slots = self.course_agent.get_free_slots(date_str, wake, sleep, current_week)
            fixed = self.routine_agent.fixed_slots_for_date(date_str)
            usable = self._subtract_fixed(free_slots, fixed)
            usable = self._subtract_used(usable, date_str)
            if not usable:
                day_cursor += timedelta(days=1)
                continue

            today_allow = min(
                soft_cap, sum(e - s for s, e in usable)
            )
            already = self._per_day_task_min.get((date_str, task_id), 0)
            left_allow = today_allow - already
            if left_allow <= 10:
                day_cursor += timedelta(days=1)
                continue

            filled = 0
            last_type_guess = None
            for slot_s, slot_e in usable:
                if filled >= left_allow or not remaining:
                    break
                cur = slot_s
                # 困难任务 -> 优先放精力高峰
                if is_hard and peak_lo < peak_hi and \
                        cur < peak_lo and peak_lo < slot_e:
                    cur = peak_lo
                while cur < slot_e and remaining and filled < left_allow:
                    step = remaining[0]
                    d = max(5, int(step.get("duration", 30)))
                    if cur + d > slot_e:
                        # 无法 fit 下 -> 移到下一个 slot
                        break
                    # 穿插：尽量避免连续两个同类步骤
                    is_logic = any(k in (step.get("name") or "")
                                   for k in ("刷题", "计算", "整理", "复盘", "写", "逻辑"))
                    is_memory = any(k in (step.get("name") or "")
                                    for k in ("背", "记", "预习", "复习", "知识点", "单词"))
                    if last_type_guess and is_logic and last_type_guess == "logic" and \
                            len([r for r in remaining if r is not step]) > 0:
                        # 交换：把下一个记忆类步骤提前（简单启发）
                        cand = -1
                        for ii in range(1, len(remaining)):
                            nm = remaining[ii].get("name") or ""
                            if any(k in nm for k in ("背", "记", "知识点", "单词")):
                                cand = ii
                                break
                        if cand > 0:
                            step = remaining.pop(cand)
                            remaining.insert(0, step)
                            d = max(5, int(step.get("duration", 30)))
                            is_memory = True
                            is_logic = False
                            if cur + d > slot_e:
                                # 换回来
                                step = remaining.pop(0)
                                remaining.insert(cand, step)
                                break
                    scheduled.append({
                        "name": step["name"],
                        "duration": d,
                        "date": date_str,
                        "start_time": min_to_str(cur),
                        "end_time": min_to_str(cur + d),
                        "_task_id": task_id,
                    })
                    # 记为已占用
                    self._used_slots.setdefault(date_str, []).append(
                        (cur, cur + d, task_id)
                    )
                    self._per_day_task_min[(date_str, task_id)] = \
                        self._per_day_task_min.get((date_str, task_id), 0) + d
                    cur += d
                    filled += d
                    last_type_guess = "logic" if is_logic else (
                        "memory" if is_memory else last_type_guess
                    )
                    remaining.pop(0)
            day_cursor += timedelta(days=1)
        return scheduled

    def _subtract_fixed(self, free, fixed):
        if not fixed:
            return list(free)
        result = []
        for s, e in free:
            cur = s
            for fs, fe in fixed:
                if fe <= cur or fs >= e:
                    continue
                if fs > cur:
                    result.append((cur, min(fs, e)))
                cur = max(cur, fe)
                if cur >= e:
                    break
            if cur < e:
                result.append((cur, e))
        return result

    def _subtract_used(self, free, date_str):
        used = self._used_slots.get(date_str, [])
        if not used:
            return list(free)
        used_sorted = sorted(used)
        result = []
        for s, e in free:
            cur = s
            for us, ue, _tid in used_sorted:
                if ue <= cur or us >= e:
                    continue
                if us > cur:
                    result.append((cur, min(us, e)))
                cur = max(cur, ue)
                if cur >= e:
                    break
            if cur < e:
                result.append((cur, e))
        return result


class ConflictAgent:
    """冲突检测 Agent：检测时间重叠"""

    @staticmethod
    def detect_conflicts(steps):
        by_date = {}
        for s in steps:
            d = s.get("date")
            if not d:
                continue
            by_date.setdefault(d, []).append(s)
        conflicts = []
        for d, lst in by_date.items():
            lst_sorted = sorted(lst, key=lambda x: to_min(x.get("start_time", "00:00")))
            for i in range(len(lst_sorted) - 1):
                a, b = lst_sorted[i], lst_sorted[i + 1]
                if to_min(a.get("end_time", "00:00")) > to_min(b.get("start_time", "00:00")):
                    conflicts.append({
                        "date": d,
                        "a": a.get("name"),
                        "b": b.get("name"),
                    })
        return conflicts


class BreakInsertAgent:
    """休息插入 Agent：在连续相邻的任务之间自动插入 5 分钟休息，
    并将后续任务顺延，避免时间重叠。"""

    @staticmethod
    def insert_breaks(steps):
        """在相邻步骤之间插入 5 分钟休息；如前一步被顺延导致与当前步骤重叠，先错开时间。"""
        result = []
        prev_end = None
        prev_date = None
        for s in steps:
            d = s.get("date")
            st = to_min(s.get("start_time"))
            et = to_min(s.get("end_time"))
            dur = int(s.get("duration", 0))
            if s.get("is_break"):
                result.append(s)
                prev_end = et
                prev_date = d
                continue
            # 如与前一步重叠（前一步被顺延后 start_time 变了），先向后错开
            if (prev_end is not None and prev_date == d
                    and st < prev_end):
                shift = prev_end - st
                st = prev_end
                et = st + dur
                s["start_time"] = min_to_str(st)
                s["end_time"] = min_to_str(et)
            # 相邻 -> 插入 5 分钟休息
            need_break = (prev_end is not None and prev_date == d
                          and st == prev_end)
            if need_break:
                start_break = prev_end
                end_break = prev_end + 5
                result.append({
                    "name": "☕ 休息",
                    "duration": 5,
                    "date": d,
                    "start_time": min_to_str(start_break),
                    "end_time": min_to_str(end_break),
                    "is_break": 1,
                })
                s["start_time"] = min_to_str(end_break)
                s["end_time"] = min_to_str(end_break + (et - st))
                et = end_break + (et - st)
            result.append(s)
            prev_end = et
            prev_date = d
        return result


class SummaryAgent:
    """汇总 Agent：生成每日规划报告"""

    @staticmethod
    def daily_summary(date_str, tasks, steps):
        day_steps = [s for s in steps if s.get("date") == date_str]
        total = sum(int(s.get("duration", 0)) for s in day_steps if not s.get("is_break"))
        completed = sum(int(s.get("duration", 0)) for s in day_steps if s.get("completed") and not s.get("is_break"))
        return {
            "date": date_str,
            "total_steps": len([s for s in day_steps if not s.get("is_break")]),
            "total_minutes": total,
            "completed_minutes": completed,
            "progress": (completed / total * 100) if total else 0,
        }


# ============================================================
#                    DeepSeek 调用封装
# ============================================================

def call_deepseek(messages, temperature=0.7, model="deepseek-chat", max_tokens=2048):
    if not DEEPSEEK_API_KEY:
        return None, "未配置 DEEPSEEK_API_KEY"
    try:
        r = requests.post(
            DEEPSEEK_CHAT_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=120,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, str(e)


def parse_json_from_text(text):
    if not text:
        return None
    # 尝试匹配首个 {...} 或 [...]
    m = re.search(r"(\{[\s\S]*\})|(\[[\s\S]*\])", text)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def ai_analyze_image(base64_data):
    """调用 DeepSeek VL (若可用) 解析课程表图片；
    改进：按网格切分识别，支持多课程单元格，字段分离，周次识别"""
    if not DEEPSEEK_API_KEY:
        return None, "未配置 DEEPSEEK_API_KEY"
    
    # 改进的 prompt：要求 AI 按网格切分识别
    prompt = """你是一个专业的课程表解析助手。请仔细分析图片中的课程表结构。

【重要：分网格识别】
课表通常是网格结构：横轴是星期（周一到周日），纵轴是节次/时间段。
请按每个网格单元格逐一识别：
- 如果某个单元格为空（无课程），跳过
- 如果某个单元格有多个课程（堆叠），全部列出，用数组表示

【字段分离要求】
对每个网格中的文字，请区分并只提取以下字段：
1. 课程名 (name)：只提取课程名称，忽略教师姓名
2. 星期 (day_of_week)：1=周一, 2=周二, ..., 7=周日
3. 时间段：
   - 如果图片有明确时间（如"08:00-09:40"), 直接提取
   - 如果只有节次（如"第1-2节"), 请转换为标准时间：
     * 第1-2节 → 08:00-09:40
     * 第3-4节 → 10:00-11:40
     * 第5-6节 → 14:00-15:40
     * 第7-8节 → 16:00-17:40
     * 第9-10节 → 19:00-20:40
4. 周次 (weeks)：
   - 提取周次信息，格式为 "开始周-结束周"
   - 如果有多个周次段（如"1-14周,17-18周"), 保留完整字符串
   - 如果图片未标注周次，默认返回 "1-16"

【忽略的信息】
- 教师姓名（不要提取）
- 教室/地点（可选提取到 location 字段）
- 学分、班级等其他元数据

【输出格式】
严格输出一个 JSON 数组，不要任何其他文字：

示例1（普通课表）：
[
  {"name":"高等数学","day_of_week":1,"start_time":"08:00","end_time":"09:40","weeks":"3-12","location":"教1-201"},
  {"name":"大学英语","day_of_week":2,"start_time":"10:00","end_time":"11:40","weeks":"1-16","location":""}
]

示例2（同一单元格有多个课程）：
[
  {"name":"高等数学","day_of_week":1,"start_time":"08:00","end_time":"09:40","weeks":"1-8","location":""},
  {"name":"线性代数","day_of_week":1,"start_time":"08:00","end_time":"09:40","weeks":"9-16","location":""}
]

请严格按照上述格式输出。如果某个单元格无法识别，跳过即可。
"""
    try:
        r = requests.post(
            DEEPSEEK_VL_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": DEEPSEEK_VL_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{base64_data}"},
                            },
                        ],
                    }
                ],
                "max_tokens": 4096,
            },
            timeout=180,
        )
        data = r.json()
        return data["choices"][0]["message"]["content"], None
    except Exception as e:
        return None, str(e)


# ============================================================
#                        Flask 路由
# ============================================================

@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


# ------- 课程表 -------
@app.route("/api/courses", methods=["GET"])
def list_courses():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM courses ORDER BY day_of_week, start_time").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/courses", methods=["POST"])
def add_course():
    data = request.get_json(force=True) or {}
    if not data.get("name") or data.get("day_of_week") is None:
        return jsonify({"error": "缺少必填字段"}), 400
    weeks = data.get("weeks", "1-16")
    conn = get_conn()
    conn.execute(
        "INSERT INTO courses (name, day_of_week, start_time, end_time, location, weeks) VALUES (?,?,?,?,?,?)",
        (
            data["name"], int(data["day_of_week"]),
            data.get("start_time", "08:00"),
            data.get("end_time", "09:00"),
            data.get("location", ""),
            weeks,
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/courses/batch", methods=["POST"])
def batch_courses():
    """批量导入：[{name, weeks, days:[{day_of_week, start_time, end_time, location}]}]"""
    data = request.get_json(force=True) or {}
    items = data.get("items") or []
    conn = get_conn()
    for it in items:
        weeks = it.get("weeks", "1-16")
        for d in it.get("days", []):
            conn.execute(
                "INSERT INTO courses (name, day_of_week, start_time, end_time, location, weeks) VALUES (?,?,?,?,?,?)",
                (
                    it["name"], int(d["day_of_week"]),
                    d.get("start_time", "08:00"),
                    d.get("end_time", "09:00"),
                    d.get("location", ""),
                    weeks,
                ),
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/courses/<int:cid>", methods=["DELETE"])
def del_course(cid):
    conn = get_conn()
    conn.execute("DELETE FROM courses WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


def detect_course_conflicts(courses):
    """检测课程冲突：同一时间段（星期+开始时间+结束时间）有多门课"""
    conflicts = []
    seen = {}
    for i, c in enumerate(courses):
        key = f"{c.get('day_of_week')}-{c.get('start_time')}-{c.get('end_time')}"
        if key in seen:
            conflicts.append({"index": i, "course": c, "conflicts_with": seen[key]})
        else:
            seen[key] = {"index": i, "course": c}
    return conflicts


def normalize_course_data(courses):
    """标准化课程数据：补充缺失字段，修正格式"""
    normalized = []
    for c in courses:
        # 补充缺失的周次
        if not c.get("weeks"):
            c["weeks"] = "1-16"
        
        # 确保时间段格式正确
        if not c.get("start_time"):
            c["start_time"] = "08:00"
        if not c.get("end_time"):
            c["end_time"] = "09:40"
        
        # 确保 day_of_week 是整数
        if c.get("day_of_week"):
            try:
                c["day_of_week"] = int(c["day_of_week"])
            except Exception:
                c["day_of_week"] = 1
        
        # 确保 name 存在
        if not c.get("name"):
            c["name"] = "未命名课程"
        
        normalized.append(c)
    return normalized


@app.route("/api/courses/text-parse", methods=["POST"])
def text_parse_courses():
    """文本解析课程：从课表文本中提取结构化课程信息"""
    data = request.get_json(force=True) or {}
    text = data.get("text") or ""
    if not text.strip():
        return jsonify({"error": "文本内容为空"}), 400

    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "未配置 DeepSeek API Key"}), 400

    prompt = f"""你是课程表解析专家。请从以下课表文本中提取所有课程，返回严格的 JSON 数组。

【输入文本】
{text}

【输出要求】
只输出 JSON 数组，每个元素包含：
- name: 课程名称（字符串）
- day_of_week: 星期几（数字 1-7，1=周一）
- start_time: 开始时间（HH:MM 格式）
- end_time: 结束时间（HH:MM 格式）
- weeks: 周次范围（字符串，如 "1-16" 或 "1-14,17-18"）
- location: 教室/地点（字符串，没有则为空字符串）

【周次识别规则】
- "3-12周" → weeks: "3-12"
- "1-14周,17-18周" → weeks: "1-14,17-18"
- 没有周次信息 → weeks: "1-16"

【节次转时间规则】
- 第1-2节 / 一二节 → 08:00-09:40
- 第3-4节 / 三四节 → 10:00-11:40
- 第5-6节 / 五六节 → 14:00-15:40
- 第7-8节 / 七八节 → 16:00-17:40
- 第9-10节 / 九十节 → 19:00-20:40

只返回 JSON 数组，不要任何其他文字、解释或 markdown 代码块。数组第一个字符必须是 [。"""

    try:
        r = requests.post(
            DEEPSEEK_CHAT_URL,
            headers={
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 2048,
            },
            timeout=60,
        )
        r.raise_for_status()
        data = r.json()
        content = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return jsonify({"error": f"AI 调用失败: {str(e)}"}), 500

    parsed = parse_json_from_text(content)
    if not isinstance(parsed, list):
        return jsonify({
            "parsed": [],
            "raw": content,
            "warning": "AI 未能解析为结构化数组",
            "success": False
        })

    normalized = normalize_course_data(parsed)
    conflicts = detect_course_conflicts(normalized)

    return jsonify({
        "parsed": normalized,
        "raw": content,
        "conflicts": conflicts,
        "has_conflicts": len(conflicts) > 0,
        "success": True,
        "total": len(normalized),
    })


@app.route("/api/courses/ai-preview", methods=["POST"])
def ai_preview_course():
    """识图预览：返回识别结果供前端展示，不直接导入"""
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "未上传图片"}), 400
    raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    content, err = ai_analyze_image(b64)
    if err:
        return jsonify({"error": err, "raw": content}), 400
    
    parsed = parse_json_from_text(content)
    if not isinstance(parsed, list):
        return jsonify({
            "parsed": [],
            "raw": content or "",
            "warning": "AI 未能解析为结构化数组，请人工确认",
            "success": False
        })
    
    # 标准化数据
    normalized = normalize_course_data(parsed)
    
    # 检测冲突
    conflicts = detect_course_conflicts(normalized)
    
    return jsonify({
        "parsed": normalized,
        "raw": content,
        "conflicts": conflicts,
        "has_conflicts": len(conflicts) > 0,
        "success": True,
        "total": len(normalized),
        "message": f"识别到 {len(normalized)} 门课程" + (f"，{len(conflicts)} 个时间冲突" if conflicts else "")
    })


@app.route("/api/courses/ai-confirm", methods=["POST"])
def ai_confirm_course():
    """确认导入：接收用户选择后的课程列表"""
    data = request.get_json(force=True) or {}
    courses = data.get("courses") or []
    if not courses:
        return jsonify({"error": "无课程数据"}), 400
    
    conn = get_conn()
    for c in courses:
        try:
            weeks = c.get("weeks", "1-16")
            conn.execute(
                "INSERT INTO courses (name, day_of_week, start_time, end_time, location, weeks) VALUES (?,?,?,?,?,?)",
                (
                    str(c.get("name", "")),
                    int(c.get("day_of_week", 1)),
                    str(c.get("start_time", "08:00")),
                    str(c.get("end_time", "09:00")),
                    str(c.get("location", "")),
                    weeks,
                ),
            )
        except Exception as e:
            pass
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "imported": len(courses)})


@app.route("/api/courses/ai-import", methods=["POST"])
def ai_import_course():
    """识图导入课程表（旧接口，直接导入，保留兼容）"""
    f = request.files.get("image")
    if not f:
        return jsonify({"error": "未上传图片"}), 400
    raw = f.read()
    b64 = base64.b64encode(raw).decode("utf-8")
    content, err = ai_analyze_image(b64)
    if err:
        return jsonify({"error": err, "raw": content}), 400
    parsed = parse_json_from_text(content)
    if not isinstance(parsed, list):
        return jsonify({"parsed": [], "raw": content or "", "warning": "AI 未能解析为结构化数组，请人工确认"})
    conn = get_conn()
    for c in parsed:
        try:
            weeks = c.get("weeks", "1-16")
            conn.execute(
                "INSERT INTO courses (name, day_of_week, start_time, end_time, location, weeks) VALUES (?,?,?,?,?,?)",
                (
                    str(c.get("name", "")),
                    int(c.get("day_of_week", 1)),
                    str(c.get("start_time", "08:00")),
                    str(c.get("end_time", "09:00")),
                    str(c.get("location", "")),
                    weeks,
                ),
            )
        except Exception:
            pass
    conn.commit()
    conn.close()
    return jsonify({"parsed": parsed, "raw": content})


# ------- 作息 -------
@app.route("/api/routines", methods=["GET"])
def get_routines():
    return jsonify(RoutineAgent.get_routine())


@app.route("/api/routines", methods=["POST"])
def set_routines():
    data = request.get_json(force=True) or {}
    conn = get_conn()
    # 先插入一条新的（保留最新版）
    fixed = data.get("fixed_tasks")
    if isinstance(fixed, list):
        fixed_json = json.dumps(fixed, ensure_ascii=False)
    else:
        fixed_json = "[]"
    conn.execute(
        """INSERT INTO routines (wake_up, sleep, meal_breakfast, meal_lunch, meal_dinner,
           peak_start, peak_end, fixed_tasks, entertainment)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            data.get("wake_up", "07:00"),
            data.get("sleep", "23:00"),
            data.get("meal_breakfast", "07:30-08:00"),
            data.get("meal_lunch", "12:00-12:30"),
            data.get("meal_dinner", "18:00-18:30"),
            data.get("peak_start", "09:00"),
            data.get("peak_end", "11:00"),
            fixed_json,
            data.get("entertainment", "20:00-21:00"),
        ),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/semester", methods=["GET"])
def get_semester():
    """获取学期设置和当前周数"""
    conn = get_conn()
    row = conn.execute(
        "SELECT semester_start, current_week FROM routines ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row:
        return jsonify({"semester_start": "", "current_week": 1, "computed_week": 1})
    d = dict(row)
    computed_week = 1
    if d.get("semester_start"):
        try:
            start_date = dt.strptime(d["semester_start"], "%Y-%m-%d").date()
            days_diff = (date.today() - start_date).days
            if days_diff >= 0:
                computed_week = (days_diff // 7) + 1
        except Exception:
            pass
    return jsonify({
        "semester_start": d.get("semester_start", ""),
        "current_week": d.get("current_week", 1),
        "computed_week": computed_week,
        "is_exam_week": computed_week > 16,
    })


@app.route("/api/semester", methods=["POST"])
def set_semester():
    """设置学期开始日期或手动设置当前周数"""
    data = request.get_json(force=True) or {}
    conn = get_conn()
    row = conn.execute("SELECT id FROM routines ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        conn.execute(
            "UPDATE routines SET semester_start=?, current_week=? WHERE id=?",
            (
                data.get("semester_start", ""),
                data.get("current_week", 1),
                row["id"],
            ),
        )
    conn.commit()
    conn.close()
    return get_semester()


# ------- 任务 -------
@app.route("/api/tasks", methods=["GET"])
def list_tasks():
    """返回任务列表 + 每个任务的步骤总数 / 已完成 / 逾期标签"""
    conn = get_conn()
    tasks = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()
    result = []
    today = date.today()
    for t in tasks:
        td = dict(t)
        steps = conn.execute(
            "SELECT * FROM task_steps WHERE task_id=? ORDER BY date, start_time",
            (td["id"],),
        ).fetchall()
        total = sum(int(s["duration"]) for s in steps if not s["is_break"])
        done = sum(int(s["duration"]) for s in steps if s["completed"] and not s["is_break"])
        td["steps_count"] = len([s for s in steps if not s["is_break"]])
        td["total_minutes"] = total
        td["completed_minutes"] = done
        td["progress"] = (done / total * 100) if total else 0
        try:
            due = dt.strptime(td["due_date"], "%Y-%m-%d").date()
            td["overdue"] = due < today
            td["days_left"] = (due - today).days
        except Exception:
            td["overdue"] = False
            td["days_left"] = 999
        td["priority_score"] = PriorityAgent.score(td)
        result.append(td)
    # 逾期任务置顶
    result.sort(key=lambda x: (not x["overdue"], -x["priority_score"]))
    conn.close()
    return jsonify(result)


@app.route("/api/tasks/<int:tid>", methods=["GET"])
def get_task(tid):
    conn = get_conn()
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        return jsonify({"error": "not found"}), 404
    td = dict(t)
    steps = conn.execute(
        "SELECT * FROM task_steps WHERE task_id=? ORDER BY date, start_time",
        (tid,),
    ).fetchall()
    td["steps"] = [dict(s) for s in steps]
    conn.close()
    return jsonify(td)


@app.route("/api/tasks", methods=["POST"])
def add_task():
    """添加任务 + 自动深度规划

    规则：若系统中已有任务，且新任务的 priority > 0 或属于「考试」，
    则对数据库中所有未完成任务整体重新排期，优先保证高优任务。
    """
    data = request.get_json(force=True) or {}
    name = (data.get("name") or "").strip()
    due = data.get("due_date")
    if not name or not due:
        return jsonify({"error": "任务名和截止日期必填"}), 400
    granularity = data.get("granularity") or "中"
    task_type = data.get("type") or "其他"
    priority = int(data.get("priority") or 0)
    user_steps = data.get("steps") or []  # [{name, duration}]
    force_replan = bool(data.get("force_replan", False))

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO tasks (name, due_date, type, granularity, priority) VALUES (?,?,?,?,?)",
        (name, due, task_type, granularity, priority),
    )
    task_id = cur.lastrowid

    # 汇总总时长
    total = sum(int(s.get("duration", 30)) for s in user_steps)

    # 判断是否需要整体重排
    is_high_priority = (priority >= 2) or (task_type == "考试")

    if is_high_priority or force_replan:
        # 收集所有未完成任务（含刚添加的这个）
        all_tasks_rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'done' OR status IS NULL"
        ).fetchall()
        tuples = []
        for row in all_tasks_rows:
            td = dict(row)
            tid = td["id"]
            if tid == task_id:
                broken = TaskBreakdownAgent.breakdown(td, user_steps, granularity)
            else:
                existing = conn.execute(
                    "SELECT name, duration FROM task_steps "
                    "WHERE task_id=? AND is_break=0 AND completed=0 "
                    "ORDER BY date, start_time",
                    (tid,),
                ).fetchall()
                if not existing:
                    continue
                broken = [
                    {"name": r["name"], "duration": int(r["duration"])}
                    for r in existing
                ]
            tuples.append((td, broken, tid))
        scheduler = SchedulingAgent()
        current_week = get_current_week()
        result_map = scheduler.schedule_multi(tuples, current_week)

        # 先清除所有未完成任务的旧步骤
        for td, _, _t in tuples:
            conn.execute(
                "DELETE FROM task_steps WHERE task_id=? AND completed=0",
                (td["id"],),
            )

        # 插入新排期
        scheduled_for_new = []
        for td, _, _t in tuples:
            rows = result_map.get(td["id"], [])
            # schedule_multi 内部已插入休息，这里直接写入
            for s in rows:
                conn.execute(
                    """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
                       VALUES (?,?,?,?,?,?,?)""",
                    (
                        td["id"], s["name"], s["date"], s["start_time"],
                        s["end_time"], int(s["duration"]), int(s.get("is_break", 0)),
                    ),
                )
            if td["id"] == task_id:
                scheduled_for_new = rows

        all_flat = []
        for _k, v in result_map.items():
            all_flat.extend(v)
        conflicts = ConflictAgent.detect_conflicts(all_flat)
        conn.commit()
        conn.close()
        return jsonify({
            "ok": True,
            "task_id": task_id,
            "total_minutes": total,
            "steps_count": len([s for s in scheduled_for_new if not s.get("is_break")]),
            "rescheduled_all": True,
            "conflicts": conflicts,
        })

    # 普通任务：只排该任务本身（不影响其他任务已完成步骤；避开其他任务未完成的时段）
    # 构造一个轻量的整体排期：把已存在的其他未完成步骤作为已占用时隙
    other_rows = conn.execute(
        """SELECT s.*, t.due_date, t.type, t.priority
           FROM task_steps s JOIN tasks t ON t.id = s.task_id
           WHERE s.task_id != ? AND s.is_break = 0 AND s.completed = 0""",
        (task_id,),
    ).fetchall()
    scheduler = SchedulingAgent()
    # 把其他任务已存在的非休息未完成步骤当作已占用
    for r in other_rows:
        d = r["date"]
        if not d:
            continue
        try:
            s_min = to_min(r["start_time"]) if r["start_time"] else 0
            e_min = to_min(r["end_time"]) if r["end_time"] else (s_min + int(r["duration"]))
        except Exception:
            continue
        scheduler._used_slots.setdefault(d, []).append(
            (s_min, e_min, r["task_id"])
        )

    new_task_broken = TaskBreakdownAgent.breakdown(
        {"name": name, "type": task_type, "due_date": due, "granularity": granularity},
        user_steps, granularity,
    )
    scheduled = scheduler.schedule(
        {"name": name, "type": task_type, "due_date": due},
        new_task_broken,
        task_id=task_id,
    )
    # schedule() 内部已经通过 schedule_multi 插入休息，这里无需重复调用
    conflicts = ConflictAgent.detect_conflicts(scheduled)

    for s in scheduled:
        conn.execute(
            """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
               VALUES (?,?,?,?,?,?,?)""",
            (
                task_id, s["name"], s["date"], s["start_time"], s["end_time"],
                int(s["duration"]), int(s.get("is_break", 0)),
            ),
        )
    conn.commit()
    conn.close()
    return jsonify({
        "ok": True,
        "task_id": task_id,
        "total_minutes": total,
        "steps_count": len([s for s in scheduled if not s.get("is_break")]),
        "conflicts": conflicts,
    })


@app.route("/api/tasks/<int:tid>/steps", methods=["PUT"])
def update_steps(tid):
    """批量更新子步骤（增/改/删）"""
    data = request.get_json(force=True) or {}
    steps = data.get("steps") or []  # 每个步骤包含 id(可选) / name / date / start_time / end_time / duration / completed
    conn = get_conn()
    # 先删除原步骤中未在更新列表里的（按 id 精确处理：缺失 id 视为新增）
    existing_ids = [int(r["id"]) for r in
                    conn.execute("SELECT id FROM task_steps WHERE task_id=?", (tid,)).fetchall()]
    keep_ids = set()
    for s in steps:
        if s.get("id"):
            keep_ids.add(int(s["id"]))
    for eid in existing_ids:
        if eid not in keep_ids:
            conn.execute("DELETE FROM task_steps WHERE id=?", (eid,))
    for s in steps:
        if s.get("id"):
            conn.execute(
                """UPDATE task_steps SET name=?, date=?, start_time=?, end_time=?, duration=?, completed=?
                   WHERE id=?""",
                (
                    s.get("name", ""), s.get("date"), s.get("start_time"), s.get("end_time"),
                    int(s.get("duration", 30)), int(s.get("completed", 0)),
                    int(s["id"]),
                ),
            )
        else:
            conn.execute(
                """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, completed, is_break)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (
                    tid, s.get("name", ""), s.get("date"), s.get("start_time"), s.get("end_time"),
                    int(s.get("duration", 30)), int(s.get("completed", 0)),
                ),
            )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/<int:tid>/replan", methods=["POST"])
def replan_task(tid):
    """智能重排：将数据库中所有未完成任务整体重新排期，
    保留所有已完成 (completed=1) 的步骤不动。"""
    conn = get_conn()
    t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        return jsonify({"error": "not found"}), 404

    # 收集所有未完成任务的「未完成步骤」
    all_tasks_rows = conn.execute(
        "SELECT * FROM tasks WHERE status != 'done' OR status IS NULL"
    ).fetchall()
    tuples = []
    for row in all_tasks_rows:
        td = dict(row)
        existing = conn.execute(
            "SELECT name, duration FROM task_steps "
            "WHERE task_id=? AND is_break=0 AND completed=0 "
            "ORDER BY date, start_time",
            (td["id"],),
        ).fetchall()
        if not existing:
            continue
        broken = [
            {"name": r["name"], "duration": int(r["duration"])}
            for r in existing
        ]
        tuples.append((td, broken, td["id"]))

    scheduler = SchedulingAgent()
    current_week = get_current_week()
    result_map = scheduler.schedule_multi(tuples, current_week)

    # 删除所有未完成步骤
    for td, _, _t in tuples:
        conn.execute(
            "DELETE FROM task_steps WHERE task_id=? AND completed=0",
            (td["id"],),
        )
    # 插入新排期
    rescheduled_count = 0
    for td, _, _t in tuples:
        rows = result_map.get(td["id"], [])
        rows_with_break = BreakInsertAgent.insert_breaks(rows)
        for s in rows_with_break:
            conn.execute(
                """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    td["id"], s["name"], s["date"], s["start_time"],
                    s["end_time"], int(s["duration"]), int(s.get("is_break", 0)),
                ),
            )
            rescheduled_count += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "rescheduled": rescheduled_count})


@app.route("/api/tasks/<int:tid>", methods=["DELETE"])
def delete_task(tid):
    conn = get_conn()
    conn.execute("DELETE FROM tasks WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/tasks/step/<int:sid>/complete", methods=["POST"])
def toggle_step(sid):
    data = request.get_json(force=True) or {}
    completed = int(data.get("completed", 1))
    conn = get_conn()
    conn.execute("UPDATE task_steps SET completed=? WHERE id=?", (completed, sid))
    conn.commit()
    # 更新任务状态
    row = conn.execute("SELECT task_id FROM task_steps WHERE id=?", (sid,)).fetchone()
    if row:
        tid = row["task_id"]
        steps = conn.execute("SELECT completed FROM task_steps WHERE task_id=? AND is_break=0", (tid,)).fetchall()
        if steps and all(s["completed"] for s in steps):
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        else:
            conn.execute("UPDATE tasks SET status='pending' WHERE id=?", (tid,))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ------- 日历视图 / 今日日程 -------
@app.route("/api/calendar", methods=["GET"])
def calendar_view():
    """返回指定日期范围内的所有步骤（按日期分组）"""
    start = request.args.get("start") or today_str()
    end = request.args.get("end") or (date.today() + timedelta(days=14)).strftime("%Y-%m-%d")
    conn = get_conn()
    rows = conn.execute(
        """SELECT ts.*, t.name AS task_name, t.due_date AS task_due, t.type AS task_type
           FROM task_steps ts LEFT JOIN tasks t ON t.id=ts.task_id
           WHERE ts.date>=? AND ts.date<=?
           ORDER BY ts.date, ts.start_time""",
        (start, end),
    ).fetchall()
    by_date = {}
    for r in rows:
        d = r["date"]
        by_date.setdefault(d, []).append(dict(r))
    # 加入课程
    # 生成日期列表
    cur_d = dt.strptime(start, "%Y-%m-%d").date()
    end_d = dt.strptime(end, "%Y-%m-%d").date()
    while cur_d <= end_d:
        ds = cur_d.strftime("%Y-%m-%d")
        courses = CourseAgent.get_courses_for_date(ds)
        for c in courses:
            c["is_course"] = 1
            by_date.setdefault(ds, []).append(c)
        cur_d += timedelta(days=1)
    for d in by_date:
        by_date[d].sort(key=lambda x: to_min(x.get("start_time") or "00:00"))
    conn.close()
    return jsonify(by_date)


@app.route("/api/today", methods=["GET"])
def today_summary():
    t = request.args.get("date", today_str())
    current_week = get_current_week()
    conn = get_conn()
    rows = conn.execute(
        """SELECT ts.*, t.name AS task_name, t.due_date AS task_due, t.type AS task_type, t.id AS task_id
           FROM task_steps ts LEFT JOIN tasks t ON t.id=ts.task_id
           WHERE ts.date=? ORDER BY ts.start_time""",
        (t,),
    ).fetchall()
    steps = [dict(r) for r in rows]
    courses = CourseAgent.get_courses_for_date(t, current_week)
    total = sum(int(s["duration"]) for s in steps if not s.get("is_break"))
    done = sum(int(s["duration"]) for s in steps if s.get("completed") and not s.get("is_break"))
    conn.close()
    return jsonify({
        "date": t,
        "steps": steps,
        "courses": courses,
        "total_minutes": total,
        "completed_minutes": done,
        "progress": (done / total * 100) if total else 0,
        "current_week": current_week,
        "is_exam_week": current_week > 16,
    })


# ------- 番茄钟 -------
@app.route("/api/pomodoro", methods=["POST"])
def record_pomodoro():
    data = request.get_json(force=True) or {}
    conn = get_conn()
    conn.execute(
        "INSERT INTO pomodoro_records (task_id, step_id, duration, mode) VALUES (?,?,?,?)",
        (
            data.get("task_id") or None,
            data.get("step_id") or None,
            int(data.get("duration", 25)),
            data.get("mode", "task"),
        ),
    )
    conn.commit()
    # 若绑定 step，则标记为完成
    if data.get("step_id"):
        conn.execute("UPDATE task_steps SET completed=1 WHERE id=?", (data["step_id"],))
        conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/pomodoro/stats", methods=["GET"])
def pomodoro_stats():
    conn = get_conn()
    rows = conn.execute(
        """SELECT mode, COUNT(*) cnt, SUM(duration) total FROM pomodoro_records GROUP BY mode""",
    ).fetchall()
    stats = {"task": {"count": 0, "minutes": 0}, "free": {"count": 0, "minutes": 0}}
    for r in rows:
        key = r["mode"] if r["mode"] in stats else "free"
        stats[key]["count"] = r["cnt"]
        stats[key]["minutes"] = r["total"] or 0
    # 今日
    today = today_str()
    rows2 = conn.execute(
        "SELECT mode, COUNT(*) cnt, SUM(duration) total FROM pomodoro_records "
        "WHERE DATE(completed_at)=? GROUP BY mode", (today,),
    ).fetchall()
    today_stats = {"task": {"count": 0, "minutes": 0}, "free": {"count": 0, "minutes": 0}}
    for r in rows2:
        key = r["mode"] if r["mode"] in today_stats else "free"
        today_stats[key]["count"] = r["cnt"]
        today_stats[key]["minutes"] = r["total"] or 0
    conn.close()
    return jsonify({"all": stats, "today": today_stats})


# ------- 对话助手 -------
@app.route("/api/chat", methods=["POST"])
def chat():
    """对话规划助手：根据自然语言理解意图并执行操作"""
    data = request.get_json(force=True) or {}
    user_msg = (data.get("message") or "").strip()
    if not user_msg:
        return jsonify({"error": "消息为空"}), 400

    conn = get_conn()
    # 保存用户消息
    conn.execute("INSERT INTO chat_history (role, content) VALUES (?,?)", ("user", user_msg))

    # 取最近 10 条历史作为上下文
    history = conn.execute(
        "SELECT role, content FROM chat_history ORDER BY id DESC LIMIT 10",
    ).fetchall()
    history = list(reversed([dict(h) for h in history]))
    conn.commit()

    # 获取当前任务简要信息，给 AI 上下文
    tasks = conn.execute("SELECT id, name, due_date FROM tasks ORDER BY id").fetchall()
    task_context = "当前任务列表:\n" + "\n".join(
        f"- id={t['id']} {t['name']} 截止: {t['due_date']}" for t in tasks
    ) or "当前无任务"

    system_prompt = f"""你是一个规划助手，擅长理解自然语言并操作任务与排期。
{task_context}

用户会用中文描述：调整时间、增删步骤、重新排期、拆解任务。
请只输出一个 JSON 对象，包含 action 字段：
- action: "reply" 仅回答 (content: string)
- action: "replan" 参数: {{task_id: number}}
- action: "add_step" 参数: {{task_id: number, name: string, duration: number}}
- action: "delete_step" 参数: {{task_id: number, step_id: number}}
- action: "update_task_due" 参数: {{task_id: number, due_date: "YYYY-MM-DD"}}
- action: "add_task" 参数: {{name: string, due_date: "YYYY-MM-DD", type: string, steps: [{{name, duration}}]}}
- action: "delete_task" 参数: {{task_id: number}}

请输出单一 JSON，不要其他文字。例如：
{{"action": "replan", "params": {{"task_id": 1}}, "content": "好的，已为您重新排期任务。"}}
"""
    messages = [{"role": "system", "content": system_prompt}]
    for h in history[:-1]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_msg})

    ai_text, err = call_deepseek(messages, temperature=0.5)
    if err:
        conn.close()
        return jsonify({"reply": f"AI 调用失败：{err}", "action": None})
    parsed = parse_json_from_text(ai_text) or {"action": "reply", "content": ai_text}
    reply_text = parsed.get("content") or "已收到"
    action = parsed.get("action")
    params = parsed.get("params") or {}

    # 执行动作
    try:
        if action == "replan" and params.get("task_id"):
            tid = int(params["task_id"])
            # 下面的函数不直接调用，而是内嵌逻辑
            t = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
            if t:
                old = conn.execute(
                    "SELECT name, duration FROM task_steps WHERE task_id=? AND is_break=0 ORDER BY date, start_time",
                    (tid,),
                ).fetchall()
                user_steps = [{"name": r["name"], "duration": int(r["duration"])} for r in old]
                if user_steps:
                    broken = TaskBreakdownAgent.breakdown(dict(t), user_steps, t["granularity"])
                    scheduler = SchedulingAgent()
                    scheduled = scheduler.schedule(dict(t), broken)
                    scheduled = BreakInsertAgent.insert_breaks(scheduled)
                    conn.execute("DELETE FROM task_steps WHERE task_id=?", (tid,))
                    for s in scheduled:
                        conn.execute(
                            """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
                               VALUES (?,?,?,?,?,?,?)""",
                            (tid, s["name"], s["date"], s["start_time"], s["end_time"],
                             int(s["duration"]), int(s.get("is_break", 0))),
                        )
                    conn.commit()
        elif action == "add_step" and params.get("task_id"):
            conn.execute(
                """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
                   VALUES (?,?,?,?,?,?,0)""",
                (
                    int(params["task_id"]),
                    params.get("name", "新步骤"),
                    params.get("date"),
                    params.get("start_time"),
                    params.get("end_time"),
                    int(params.get("duration", 30)),
                ),
            )
            conn.commit()
        elif action == "delete_step" and params.get("step_id"):
            conn.execute("DELETE FROM task_steps WHERE id=?", (int(params["step_id"]),))
            conn.commit()
        elif action == "update_task_due" and params.get("task_id") and params.get("due_date"):
            conn.execute("UPDATE tasks SET due_date=? WHERE id=?",
                         (params["due_date"], int(params["task_id"])))
            conn.commit()
        elif action == "add_task" and params.get("name") and params.get("due_date"):
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO tasks (name, due_date, type, granularity) VALUES (?,?,?,?)",
                (params["name"], params["due_date"], params.get("type", "其他"), params.get("granularity", "中")),
            )
            tid = cur.lastrowid
            for s in params.get("steps") or []:
                conn.execute(
                    """INSERT INTO task_steps (task_id, name, date, start_time, end_time, duration, is_break)
                       VALUES (?,?,?,?,?,?,0)""",
                    (tid, s.get("name", "步骤"), s.get("date"), s.get("start_time"),
                     s.get("end_time"), int(s.get("duration", 30))),
                )
            conn.commit()
        elif action == "delete_task" and params.get("task_id"):
            conn.execute("DELETE FROM tasks WHERE id=?", (int(params["task_id"]),))
            conn.commit()
    except Exception as ex:
        reply_text += f" (执行失败: {ex})"

    # 保存 AI 回复
    conn.execute("INSERT INTO chat_history (role, content) VALUES (?,?)", ("assistant", reply_text))
    conn.commit()
    conn.close()
    return jsonify({"reply": reply_text, "action": action, "params": params})


@app.route("/api/chat/history", methods=["GET"])
def chat_history_list():
    conn = get_conn()
    rows = conn.execute("SELECT role, content, created_at FROM chat_history ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    return jsonify([dict(r) for r in reversed(rows)])


@app.route("/api/summary", methods=["GET"])
def summary_endpoint():
    d = request.args.get("date") or today_str()
    conn = get_conn()
    steps = conn.execute(
        "SELECT * FROM task_steps WHERE date=? ORDER BY start_time", (d,),
    ).fetchall()
    tasks = conn.execute("SELECT * FROM tasks").fetchall()
    conn.close()
    step_dicts = [dict(s) for s in steps]
    task_dicts = [dict(t) for t in tasks]
    return jsonify(SummaryAgent.daily_summary(d, task_dicts, step_dicts))


# ============================================================
#                        启动入口
# ============================================================

if __name__ == "__main__":
    print(f"规划助手启动中... 数据库: {DB_PATH}")
    print("手机访问请使用本机局域网 IP，例如 http://192.168.x.x:9090")
    app.run(host="0.0.0.0", port=9090, debug=False)
