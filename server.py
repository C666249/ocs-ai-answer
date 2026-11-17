#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OCS AI 题库服务
================================================================
给 OCS 网课助手（ocsjs）提供「自定义题库」接口，背后调用大模型作答。

零第三方依赖，只用 Python 标准库，Python 3.8+ 即可运行。

用法：
    python server.py                 用同目录 config.json 启动
    python server.py --port 8321     覆盖端口
    python server.py --mock          不调用模型，返回固定答案（自测链路用）
    python server.py --clear-cache   清空答案缓存后退出
    python server.py --stats         打印统计后退出

接口：
    GET  /query?title=&type=&options=   OCS 调用入口
    POST /query   JSON body 同上
    GET  /health  健康检查
    GET  /stats   命中率、调用次数、花费时间
    GET  /        浏览器打开的状态页
"""

import argparse
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CACHE_DB = os.path.join(DATA_DIR, "cache.db")
BANK_DB = os.path.join(DATA_DIR, "bank.db")
LOG_FILE = os.path.join(DATA_DIR, "answers.jsonl")

CFG = {}
MOCK = False
SEM = threading.Semaphore(4)
STATS_LOCK = threading.Lock()
STATS = {"total": 0, "cache_hit": 0, "bank_hit": 0, "bank_miss": 0,
         "llm_call": 0, "llm_fail": 0, "low_conf": 0, "ms_sum": 0}
# 真实 token 用量（每次模型返回的 usage 累加），用于估算花费
USAGE = {"prompt_tokens": 0, "cached_tokens": 0, "completion_tokens": 0, "calls": 0}
# 默认单价：人民币元 / 百万 tokens，对应 deepseek-chat（V4-Flash 非思考模式）
# 若换模型或官方调价，在 config.json 的 pricing 里覆盖
DEFAULT_PRICE = {"input": 1.0, "cached_input": 0.02, "output": 2.0}

# ---------------------------------------------------------------- 基础工具

def setup_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def load_config(path=CONFIG_PATH):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def ensure_data_dir():
    os.makedirs(DATA_DIR, exist_ok=True)


# ---------------------------------------------------------------- 文本归一化

TYPE_MAP = {
    "single": "single", "radio": "single", "danxuan": "single", "单选题": "single",
    "multiple": "multiple", "checkbox": "multiple", "multi": "multiple", "多选题": "multiple",
    "judgement": "judgement", "judge": "judgement", "judgment": "judgement",
    "tf": "judgement", "truefalse": "judgement", "判断题": "judgement",
    "completion": "completion", "fill": "completion", "blank": "completion", "填空题": "completion",
}

TYPE_CN = {
    "single": "单选题", "multiple": "多选题", "judgement": "判断题",
    "completion": "填空题", "unknown": "题目",
}

# 形如 "A." / "A、" / "A)" / "A．" / "A:" 的选项前缀
OPT_PREFIX = re.compile(r"^\s*([A-Za-z])\s*[.．、\)）:：]\s*")

TYPE_TAG_RE = re.compile(
    r"^\s*[【\[\(（]\s*(?:单选题?|多选题?|判断题?|填空题?|简答题?|论述题?|不定项选择题?|选择题?)\s*[】\]\)）]\s*"
)
TYPE_TAG2_RE = re.compile(r"^\s*(?:单选题?|多选题?|判断题?|填空题?)\s*[:：.、]\s*")
NUM_PREFIX_RE = re.compile(r"^\s*(?:\d{1,3}\s*[.、．\)）]|[\(（]\s*\d{1,3}\s*[\)）])\s*")


def normalize_qtype(t):
    t = (t or "").strip().lower()
    if not t:
        return "unknown"
    if t in TYPE_MAP:
        return TYPE_MAP[t]
    if t.startswith("judge") or t.startswith("judg"):
        return "judgement"
    if t.startswith("multi"):
        return "multiple"
    if t.startswith("comp") or t.startswith("fill"):
        return "completion"
    if t.startswith("sing"):
        return "single"
    return "unknown"


def clean_title(title):
    """去掉题型标记和题号，压缩空白。不碰题干里的 [图片N] 等内容。"""
    t = (title or "").strip()
    t = TYPE_TAG_RE.sub("", t)
    t = TYPE_TAG2_RE.sub("", t)
    t = NUM_PREFIX_RE.sub("", t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def parse_options(raw):
    """把 OCS 传来的 options 字符串拆成 [(字母, 文本), ...]，无字母前缀时自动补。"""
    if not raw or not raw.strip():
        return []
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [ln.strip() for ln in text.split("\n")]
    lines = [ln for ln in lines if ln]
    if not lines:
        return []

    prefixed = [ln for ln in lines if OPT_PREFIX.match(ln)]
    use_prefix = len(prefixed) >= 2 and len(prefixed) * 2 >= len(lines)

    opts = []
    if use_prefix:
        for ln in lines:
            m = OPT_PREFIX.match(ln)
            if m:
                opts.append([m.group(1).upper(), ln[m.end():].strip()])
            elif opts:
                # 图片占位符等续行，并入上一项
                opts[-1][1] = (opts[-1][1] + " " + ln).strip()
            else:
                opts.append([chr(65 + len(opts)), ln])
    else:
        for ln in lines:
            opts.append([chr(65 + len(opts)), ln])

    # 统一重新编号，保证连续 A、B、C…（原本乱序或跳号时也能对上 DOM 顺序）
    return [(chr(65 + i), txt) for i, (_letter, txt) in enumerate(opts)]


# ---------------------------------------------------------------- Prompt

SYSTEM_PROMPT = """你是中国高校在线课程平台的答题引擎，根据题干和选项给出答案。

【输出格式】
只输出一个 JSON 对象，不要解释、不要代码块标记：
{"answer": "答案", "confidence": 0.85}

【answer 字段按题型写】
- single（单选）：一个选项字母，例如 "B"
- multiple（多选）：全部正确项的字母，按字母升序、用 # 连接，例如 "A#C"。宁可少选也不要错选
- judgement（判断）：只能是 "正确" 或 "错误" 两个词之一
- completion（填空）：直接写应填入的内容；多个空按顺序用 # 连接

【作答原则】
1. 只依据题干与选项的文字信息作答，不引入题目以外的前提。
2. 题干里的 [图片1]、[图片2] 表示此处有图片但你看不到，尽量依据文字推断；若确实无法判断，把 confidence 压到 0.3 以下。
3. confidence 是你对答案正确性的真实估计（0~1），不确定就写低，不要硬猜。
4. 题干或选项缺失、乱码、无意义时，answer 写 ""，confidence 写 0。"""

NEG_HINT = {
    "judgement": '注意：answer 必须是 "正确" 或 "错误"，不能写字母。',
    "multiple": "注意：多选答案必须按字母升序并用 # 连接，例如 A#C。",
    "completion": "注意：直接写填空内容，不要写题号或“答案：”。",
}


def build_messages(title, qtype, options):
    parts = [f"题型：{TYPE_CN.get(qtype, '题目')}（{qtype}）", f"题目：{title}"]
    if options:
        parts.append("选项：")
        for letter, text in options:
            parts.append(f"{letter}. {text}")
    else:
        parts.append("选项：（无，本题为填空或判断，请直接作答）")
    hint = NEG_HINT.get(qtype)
    if hint:
        parts.append(hint)
    parts.append('请输出 JSON：{"answer": "...", "confidence": 0.0}')
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


# ---------------------------------------------------------------- 大模型调用

def chat_url(base_url):
    b = (base_url or "").strip().rstrip("/")
    if b.endswith("/chat/completions"):
        return b
    if b.endswith("/v1"):
        return b + "/chat/completions"
    return b + "/v1/chat/completions"


def deepseek_balance():
    """查询模型账户余额（仅 DeepSeek 官方 base_url 支持）。"""
    llm = CFG.get("llm", {})
    base = str(llm.get("base_url", "")).rstrip("/")
    if "deepseek.com" not in base:
        return {"ok": False, "msg": "当前 base_url 不支持余额查询"}
    req = urllib.request.Request(
        base + "/user/balance",
        headers={"Authorization": "Bearer " + str(llm.get("api_key", "")),
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            p = json.loads(resp.read().decode("utf-8", errors="replace"))
        info = (p.get("balance_infos") or [{}])[0]
        return {"ok": True, "available": p.get("is_available"),
                "total": info.get("total_balance"),
                "granted": info.get("granted_balance"),
                "topped_up": info.get("topped_up_balance"),
                "currency": info.get("currency")}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


def call_llm(messages, timeout, temperature=None):
    llm = CFG.get("llm", {})
    body = {
        "model": llm.get("model", "deepseek-chat"),
        "messages": messages,
        "temperature": llm.get("temperature", 0.2) if temperature is None else temperature,
        "max_tokens": llm.get("max_tokens", 300),
        "stream": False,
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        chat_url(llm.get("base_url", "https://api.deepseek.com")),
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + str(llm.get("api_key", "")),
            "Accept": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    u = payload.get("usage") or {}
    try:
        hit = int(u.get("prompt_cache_hit_tokens")
                  or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        with STATS_LOCK:
            USAGE["calls"] += 1
            USAGE["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
            USAGE["completion_tokens"] += int(u.get("completion_tokens") or 0)
            USAGE["cached_tokens"] += hit
    except Exception:
        pass
    return payload["choices"][0]["message"]["content"]


def extract_json(text):
    if not text:
        return None
    s = str(text).strip()
    m = re.search(r"```(?:json)?\s*(.+?)\s*```", s, re.S)
    if m:
        s = m.group(1).strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    for pat in (r"\{.*\}", r"\{.*?\}"):
        m = re.search(pat, s, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
    # 完全没 JSON 时，把整段当作答案文本交给后面的归一化处理
    return {"answer": s, "confidence": 0.4}


# ---------------------------------------------------------------- 答案归一化

CORRECT_WORDS = {"正确", "对", "是", "对的", "是的", "正确的", "√", "true", "t", "yes", "1"}
WRONG_WORDS = {"错误", "错", "否", "×", "x", "错的", "不对", "不正确", "不是", "false", "f", "no", "0"}
ANSWER_PREFIX_RE = re.compile(r"^\s*(?:正确答案|答案|answer)\s*[:：]\s*", re.I)
LETTER_RE = re.compile(r"[A-Z]")


def _norm_text(s):
    """粗归一化，用于把选项文本和答案文本对比。"""
    s = (s or "").lower()
    s = re.sub(r"[\s\u3000]+", "", s)
    s = re.sub(r"[，。、；：,.;:!?！？“”\"'（）()\[\]【】<>《》\-—_/\\]", "", s)
    return s


def plain_letters(s):
    """仅当整段答案就是 A/B/C 这类纯字母时才返回字母，避免从英文单词里误抓字母。"""
    t = re.sub(r"[\s,，.。．、#;；|/]", "", str(s or "")).upper()
    if t and len(t) <= 8 and re.fullmatch(r"[A-Z]+", t):
        return sorted(set(t))
    return []


def match_option_by_text(text, options):
    """答案给的是单个选项文本时，反查字母。"""
    t = _norm_text(text)
    if not t or not options:
        return None
    for letter, opt in options:
        o = _norm_text(opt)
        if not o:
            continue
        if o == t or (len(t) >= 4 and t in o):
            return letter
    return None


def match_options_by_text(text, options):
    """多选：答案给的是若干选项文本（如「甲和丙」）时，反查出全部字母。"""
    t = _norm_text(text)
    if not t or not options:
        return []
    hits = []
    for letter, opt in options:
        o = _norm_text(opt)
        if not o:
            continue
        if o == t or (len(o) >= 2 and o in t) or (len(o) == 1 and len(t) <= 8 and o in t):
            hits.append(letter)
    if len(hits) > 1:
        norm = {l: _norm_text(d) for l, d in options}
        # 某个命中项若被另一个命中项包含（「甲」vs「甲乙」），只保留更长的那个
        hits = [
            l for l in hits
            if not any(norm[l] != norm[k] and norm[l] in norm[k] for k in hits if k != l)
        ]
    return hits


def normalize_answer(raw, qtype, options):
    """把模型的自由输出收敛成 OCS 能识别的答案格式。"""
    s = str(raw if raw is not None else "").strip()
    s = ANSWER_PREFIX_RE.sub("", s).strip()
    s = s.strip("\"'“”‘’「」").strip()

    if not s:
        return ""

    if qtype == "judgement":
        low = s.lower().strip()
        if low in CORRECT_WORDS:
            return "正确"
        if low in WRONG_WORDS:
            return "错误"
        # 形如 "A" / "B" 时按选项文本判断
        letters = LETTER_RE.findall(s.upper())
        if len(letters) == 1 and options:
            idx = ord(letters[0]) - 65
            if 0 <= idx < len(options):
                ot = options[idx][1].strip().lower()
                if ot in CORRECT_WORDS or any(w == ot for w in CORRECT_WORDS):
                    return "正确"
                if ot in WRONG_WORDS or any(w == ot for w in WRONG_WORDS):
                    return "错误"
        # 兜底：句子里出现了"正确/错误"类词
        if any(w in s for w in ("正确", "对的", "是的", "√")):
            return "正确"
        if any(w in s for w in ("错误", "错的", "不对", "不正确", "×")):
            return "错误"
        return s

    if qtype == "multiple":
        letters = plain_letters(s)
        if len(letters) >= 2:
            return "#".join(letters)
        texts = match_options_by_text(s, options)
        if len(texts) >= 2:
            return "#".join(texts)
        if len(texts) == 1 and not letters:
            return texts[0]
        if len(letters) == 1:
            return letters[0]
        return match_option_by_text(s, options) or s

    if qtype == "single":
        letters = plain_letters(s)
        if len(letters) == 1:
            return letters[0]
        by_text = match_option_by_text(s, options)
        if by_text:
            return by_text
        if len(letters) > 1:
            return letters[0]
        rest = LETTER_RE.findall(s.upper())
        return rest[0] if rest else s

    if qtype == "completion":
        s = re.sub(r"^\s*(?:答案|正确答案)\s*[:：]\s*", "", s)
        # 保留 # 作为多空分隔，其余分隔符统一成 #
        s = re.sub(r"\s*(?:\r?\n|；|;|\||,|，|、)+\s*", "#", s)
        s = re.sub(r"#{2,}", "#", s).strip("#").strip()
        s = re.sub(r"^[\"'“”]+|[\"'“”]+$", "", s).strip()
        return s

    # unknown：能识别成字母就返回字母，否则返回原文
    letters = LETTER_RE.findall(s.upper())
    if len(letters) == 1:
        return letters[0]
    return s


# ---------------------------------------------------------------- 缓存

def db_conn():
    ensure_data_dir()
    conn = sqlite3.connect(CACHE_DB, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS answers (
               key TEXT PRIMARY KEY,
               title TEXT, qtype TEXT, options TEXT,
               answer TEXT, confidence REAL, model TEXT,
               hits INTEGER DEFAULT 0,
               created_at INTEGER, updated_at INTEGER
           )"""
    )
    conn.commit()
    return conn


def cache_key(title, qtype, options):
    raw = "|".join([_norm_text(title), qtype, _norm_text(" ".join(o[1] for o in options))])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(key):
    conn = None
    try:
        conn = db_conn()
        row = conn.execute(
            "SELECT answer, confidence FROM answers WHERE key=?", (key,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE answers SET hits=hits+1, updated_at=? WHERE key=?",
                (int(time.time()), key),
            )
            conn.commit()
            return {"answer": row[0], "confidence": row[1]}
    except Exception as e:
        log(f"缓存读取失败: {e}")
    finally:
        if conn:
            conn.close()
    return None


def cache_put(key, title, qtype, options, answer, confidence, model):
    conn = None
    try:
        now = int(time.time())
        conn = db_conn()
        conn.execute(
            """INSERT INTO answers (key,title,qtype,options,answer,confidence,model,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 answer=excluded.answer, confidence=excluded.confidence,
                 model=excluded.model, updated_at=excluded.updated_at""",
            (key, title[:500], qtype, " | ".join(o[1] for o in options)[:800],
             answer, confidence, model, now, now),
        )
        conn.commit()
    except Exception as e:
        log(f"缓存写入失败: {e}")
    finally:
        if conn:
            conn.close()


def cache_stats():
    conn = None
    try:
        conn = db_conn()
        total = conn.execute("SELECT COUNT(*) FROM answers").fetchone()[0]
        hit = conn.execute("SELECT COALESCE(SUM(hits),0) FROM answers").fetchone()[0]
        return {"entries": total, "hits": hit}
    except Exception:
        return {"entries": 0, "hits": 0}
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------- 本地题库
#
# 与「AI 缓存」是两套东西：
#   cache.db  = 按「题干+选项」哈希缓存 AI 的答案，选项顺序一变就失效
#   bank.db   = 人工/抓取来的正确答案，只按「题干」定位，选项顺序变了也能自动换算
#
# 这正是二次作答能满分的原因：老师换了选项顺序，字母要重新映射，不能照抄。

BANK_LOCK = threading.Lock()

# 题库答案的来源优先级：数字越大越可信
BANK_SOURCE_RANK = {"official": 3, "manual": 2, "ai": 1}


def bank_conn():
    ensure_data_dir()
    conn = sqlite3.connect(BANK_DB, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bank (
               key TEXT PRIMARY KEY,
               title TEXT, title_norm TEXT, qtype TEXT,
               options TEXT, answer TEXT,
               source TEXT DEFAULT 'official',
               verified INTEGER DEFAULT 1,
               hits INTEGER DEFAULT 0,
               created_at INTEGER, updated_at INTEGER
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bank_norm ON bank(title_norm)")
    conn.commit()
    return conn


def bank_key(title, qtype):
    """题库主键只看题干+题型，不含选项 —— 这样选项乱序也能命中。"""
    raw = _norm_text(title) + "|" + (qtype or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _title_norm(title):
    """题干归一化：去题号、去空白、去标点，用于模糊比对。"""
    t = clean_title(title)
    t = _norm_text(t)
    return t


def _find_opt_by_text(txt, cur_options, strict=0.9):
    """在当前选项里找与 txt 文本最接近的一项，返回字母。"""
    n = _norm_text(txt)
    if not n:
        return None
    best, best_score = None, 0.0
    for letter, t in cur_options:
        tn = _norm_text(t)
        if not tn:
            continue
        if tn == n:
            return letter
        r = difflib.SequenceMatcher(None, n, tn).ratio()
        if r > best_score:
            best, best_score = letter, r
    return best if best_score >= strict else None


def remap_answer(bank_answer, bank_options, cur_options, qtype):
    """把题库里的字母答案换算到当前页面的选项顺序上。

    返回 (answer, ok)。ok=False 表示无法确定对应关系（题目被改过、或没抓到选项），
    此时必须判为「未命中」，绝不能硬填一个字母上去。
    """
    a = str(bank_answer or "").strip()
    letters = [x.strip().upper() for x in a.split("#") if x.strip()]
    is_letters = bool(letters) and all(re.fullmatch(r"[A-H]", x) for x in letters)

    # 文字型答案（正确/错误/填空内容）跟选项顺序无关，直接可用
    if not is_letters:
        return a, True
    if qtype in ("completion", "short", "fill"):
        return a, True

    # 字母答案必须有选项文字作对照，否则无从判断顺序有没有变 —— 宁可转 AI
    if not bank_options or not cur_options:
        return a, False

    bank_texts = [t for _, t in bank_options]
    cur_texts = [t for _, t in cur_options]
    if bank_texts == cur_texts:
        # 顺序一致，直接用。但仍要按解析后的字母重拼：历史入库数据里出现过
        # "A##B#C#D" 这种带空项的答案，原样返回会让答题脚本多拆出一个空选项。
        uniq = []
        for letter in letters:
            if letter not in uniq:
                uniq.append(letter)
        return "#".join(uniq), True

    bmap = {l: t for l, t in bank_options}
    out = []
    for letter in [x for x in str(bank_answer).split("#") if x.strip()]:
        txt = bmap.get(letter.strip())
        if txt is None:
            return bank_answer, False
        hit = _find_opt_by_text(txt, cur_options)
        if not hit:
            return bank_answer, False
        out.append(hit)
    if not out:
        return bank_answer, False
    out = sorted(set(out))
    if qtype == "single" and len(out) > 1:
        return bank_answer, False
    return "#".join(out), True


def bank_put(title, qtype, options, answer, source="official", verified=1):
    """写题库。已存在的条目按来源优先级决定是否覆盖（official 不会被 ai 顶掉）。"""
    title = clean_title(title)
    if not title or not str(answer or "").strip():
        return {"ok": False, "msg": "题干或答案为空"}
    # 只拿到字母、没有选项文本 —— 等于埋了一颗「换顺序就填错」的雷。
    # 无论题型，一律拒绝：字母答案必须能对上选项原文才准入库。
    if re.fullmatch(r"[A-H](\s*#\s*[A-H])*", str(answer).strip().upper()) and len(options or []) < 2:
        return {"ok": False, "msg": "字母答案缺选项文本，拒绝入库（无法保证选项顺序对应）"}
    key = bank_key(title, qtype or "unknown")
    now = int(time.time())
    opts_json = json.dumps(options or [], ensure_ascii=False)
    conn = None
    try:
        with BANK_LOCK:
            conn = bank_conn()
            row = conn.execute("SELECT source FROM bank WHERE key=?", (key,)).fetchone()
            if row:
                old = str(row[0] or "manual")
                new_rank = BANK_SOURCE_RANK.get(source, 0)
                old_rank = BANK_SOURCE_RANK.get(old, 0)
                # 官方答案已存在时，不再被 AI 答案覆盖
                if old_rank > new_rank and source == "ai":
                    return {"ok": True, "skipped": True, "reason": "已有更可信的答案"}
                conn.execute(
                    """UPDATE bank SET title=?, title_norm=?, qtype=?, options=?, answer=?,
                       source=?, verified=?, updated_at=? WHERE key=?""",
                    (title[:800], _title_norm(title), qtype or "unknown", opts_json,
                     str(answer), source, int(verified), now, key),
                )
            else:
                conn.execute(
                    """INSERT INTO bank (key,title,title_norm,qtype,options,answer,
                       source,verified,hits,created_at,updated_at)
                       VALUES (?,?,?,?,?,?,?,?,0,?,?)""",
                    (key, title[:800], _title_norm(title), qtype or "unknown", opts_json,
                     str(answer), source, int(verified), now, now),
                )
            conn.commit()
        return {"ok": True, "key": key}
    except Exception as e:
        log(f"题库写入失败: {e}")
        return {"ok": False, "msg": str(e)}
    finally:
        if conn:
            conn.close()


def bank_query(title, qtype, cur_options):
    """查题库。先精确命中，再按题干相似度兜底。返回 None 表示确实没有这道题。"""
    route = CFG.get("route", {}) or {}
    threshold = float(route.get("match_threshold", 0.95))
    exact_only = bool(route.get("exact_only", True))
    allow_unverified = bool(route.get("use_unverified", True))

    norm = _title_norm(title)
    if not norm:
        return None

    conn = None
    try:
        conn = bank_conn()
        key = bank_key(title, qtype or "unknown")
        row = conn.execute(
            "SELECT title,answer,options,source,verified FROM bank WHERE key=?", (key,)
        ).fetchone()

        if not row:
            # 默认只认完全一致的题干。题目被老师改过一个字就不算命中，
            # 免得拿旧答案去填新题 —— 宁可转 AI，也不硬抄。
            if exact_only:
                return None
            rows = conn.execute(
                "SELECT title,title_norm,answer,options,source,verified FROM bank WHERE qtype=?",
                (qtype or "unknown",),
            ).fetchall()
            best, best_score = None, 0.0
            for r in rows:
                tn = str(r[1] or "")
                if not tn:
                    continue
                # 长度差太多直接跳过，省时间
                if len(tn) > len(norm) * 1.6 or len(norm) > len(tn) * 1.6:
                    continue
                score = difflib.SequenceMatcher(None, norm, tn).ratio()
                if score > best_score:
                    best, best_score = r, score
            if not best or best_score < threshold:
                return None
            row = best

        stored_title, answer, opts_json, source, verified = row[0], row[1], row[2], row[3], row[4]
        if not allow_unverified and not int(verified or 0):
            return None

        try:
            bank_options = json.loads(opts_json or "[]")
        except Exception:
            bank_options = []

        fixed, ok = remap_answer(answer, bank_options, cur_options, qtype or "unknown")
        if not ok:
            log(f"题库命中但选项对不上，判定未命中：{stored_title[:30]}")
            return None

        conn.execute(
            "UPDATE bank SET hits=hits+1, updated_at=? WHERE key=?",
            (int(time.time()), bank_key(stored_title, qtype or "unknown")),
        )
        conn.commit()
        return {"answer": fixed, "source": source or "manual",
                "confidence": 1.0 if int(verified or 0) else 0.7,
                "title": stored_title}
    except Exception as e:
        log(f"题库查询失败: {e}")
        return None
    finally:
        if conn:
            conn.close()


def bank_stats():
    conn = None
    try:
        conn = bank_conn()
        total = conn.execute("SELECT COUNT(*) FROM bank").fetchone()[0]
        verified = conn.execute("SELECT COUNT(*) FROM bank WHERE verified=1").fetchone()[0]
        hits = conn.execute("SELECT COALESCE(SUM(hits),0) FROM bank").fetchone()[0]
        by_src = dict(conn.execute(
            "SELECT source, COUNT(*) FROM bank GROUP BY source").fetchall())
        return {"entries": total, "verified": verified, "hits": hits, "by_source": by_src}
    except Exception:
        return {"entries": 0, "verified": 0, "hits": 0, "by_source": {}}
    finally:
        if conn:
            conn.close()


def bank_list(limit=200, offset=0):
    conn = None
    try:
        conn = bank_conn()
        rows = conn.execute(
            """SELECT title,qtype,answer,source,verified,hits,updated_at
               FROM bank ORDER BY updated_at DESC LIMIT ? OFFSET ?""",
            (int(limit), int(offset)),
        ).fetchall()
        return [{"title": r[0], "type": r[1], "answer": r[2], "source": r[3],
                 "verified": r[4], "hits": r[5], "updated_at": r[6]} for r in rows]
    except Exception:
        return []
    finally:
        if conn:
            conn.close()


def bank_delete(key_title, qtype=None):
    conn = None
    try:
        with BANK_LOCK:
            conn = bank_conn()
            if qtype:
                cur = conn.execute("DELETE FROM bank WHERE key=?",
                                   (bank_key(key_title, qtype),))
            else:
                cur = conn.execute("DELETE FROM bank WHERE key=?",
                                   (bank_key(key_title, "unknown"),))
            conn.commit()
            return {"ok": True, "deleted": cur.rowcount}
    except Exception as e:
        return {"ok": False, "msg": str(e)}
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------- 日志

def write_log(rec):
    try:
        ensure_data_dir()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- 核心流程

ROUTE_MODES = ("hybrid", "bank_only", "ai_only")

MODE_DESC = {
    "hybrid": "本地题库优先，没命中才走 AI",
    "bank_only": "只用本地题库，没命中就留空（不调 AI、不硬填）",
    "ai_only": "只走 AI，完全不查本地题库",
}


def get_route_mode():
    mode = str((CFG.get("route", {}) or {}).get("mode", "hybrid")).lower().strip()
    return mode if mode in ROUTE_MODES else "hybrid"


def answer_question(title, qtype, raw_options):
    t0 = time.time()
    qtype = normalize_qtype(qtype)
    clean = clean_title(title)
    opts = parse_options(raw_options)

    if not clean:
        return {"code": 0, "msg": "题目为空"}

    mode = get_route_mode()

    # ---- 第一程：本地题库
    if mode in ("hybrid", "bank_only"):
        hit = bank_query(clean, qtype, opts)
        if hit:
            el = int((time.time() - t0) * 1000)
            with STATS_LOCK:
                STATS["total"] += 1
                STATS["bank_hit"] += 1
                STATS["ms_sum"] += el
            write_log({"t": int(time.time()), "title": clean, "type": qtype,
                       "answer": hit["answer"], "source": "bank",
                       "bank_source": hit.get("source"), "ms": el})
            return {"code": 1, "question": clean, "answer": hit["answer"],
                    "ai": False, "source": "bank", "bank_source": hit.get("source"),
                    "confidence": hit.get("confidence", 1.0),
                    "cached": False, "ms": el}

        # 仅本地模式：没命中就必须留空，绝不硬凑
        if mode == "bank_only":
            el = int((time.time() - t0) * 1000)
            with STATS_LOCK:
                STATS["total"] += 1
                STATS["bank_miss"] += 1
                STATS["ms_sum"] += el
            write_log({"t": int(time.time()), "title": clean, "type": qtype,
                       "source": "bank_miss", "ms": el})
            return {"code": 0,
                    "msg": "本地题库没有这题（当前为「仅本地」模式，未调用 AI）"}

    # ---- 第二程：AI 兜底
    return _answer_ai(clean, qtype, opts, t0)


def _answer_ai(clean, qtype, opts, t0):
    key = cache_key(clean, qtype, opts)
    behavior = CFG.get("behavior", {})
    use_cache = behavior.get("cache_enabled", True)

    if use_cache:
        hit = cache_get(key)
        if hit and hit.get("answer"):
            el = int((time.time() - t0) * 1000)
            with STATS_LOCK:
                STATS["total"] += 1
                STATS["cache_hit"] += 1
                STATS["ms_sum"] += el
            write_log({"t": int(time.time()), "title": clean, "type": qtype,
                       "answer": hit["answer"], "cached": True, "ms": el})
            return {"code": 1, "question": clean, "answer": hit["answer"],
                    "ai": True, "confidence": hit.get("confidence"), "cached": True, "ms": el}

    msgs = build_messages(clean, qtype, opts)
    llm = CFG.get("llm", {})
    model = llm.get("model", "deepseek-chat")
    retry = int(behavior.get("max_retry", 1))
    threshold = float(behavior.get("confidence_threshold", 0.35))

    if MOCK:
        fake = {"single": {"answer": "A", "confidence": 0.99},
                "multiple": {"answer": "A#C", "confidence": 0.9},
                "judgement": {"answer": "正确", "confidence": 0.9},
                "completion": {"answer": "示例填空", "confidence": 0.6}}.get(qtype, {"answer": "A", "confidence": 0.5})
        el = int((time.time() - t0) * 1000)
        return {"code": 1, "question": clean, "answer": normalize_answer(fake["answer"], qtype, opts),
                "ai": True, "confidence": fake["confidence"], "cached": False,
                "mock": True, "ms": el}

    parsed = None
    last_err = None
    timeouts = [llm.get("timeout", 35)]
    for _ in range(max(0, retry)):
        timeouts.append(min(15, llm.get("timeout", 35)))
    for attempt, to in enumerate(timeouts):
        try:
            if not SEM.acquire(timeout=25):
                last_err = "服务繁忙，排队超时"
                break
            try:
                raw = call_llm(msgs, to, temperature=llm.get("temperature", 0.2) + 0.1 * attempt)
            finally:
                SEM.release()
            with STATS_LOCK:
                STATS["llm_call"] += 1
            parsed = extract_json(raw)
            if parsed and str(parsed.get("answer", "")).strip():
                break
            last_err = "模型返回空答案"
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            last_err = f"接口 HTTP {e.code} {body}"
            log(last_err)
            if e.code in (401, 402, 403, 404):
                break
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            log(f"模型调用失败（第 {attempt + 1} 次）: {last_err}")

    if not parsed:
        with STATS_LOCK:
            STATS["total"] += 1
            STATS["llm_fail"] += 1
            STATS["ms_sum"] += int((time.time() - t0) * 1000)
        write_log({"t": int(time.time()), "title": clean, "type": qtype,
                   "error": last_err, "ms": int((time.time() - t0) * 1000)})
        return {"code": 0, "msg": f"AI 调用失败：{last_err or '未知错误'}"}

    answer = normalize_answer(parsed.get("answer"), qtype, opts)
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except Exception:
        confidence = 0.5

    el = int((time.time() - t0) * 1000)
    with STATS_LOCK:
        STATS["total"] += 1
        STATS["ms_sum"] += el

    if not answer:
        with STATS_LOCK:
            STATS["low_conf"] += 1
        return {"code": 0, "msg": "AI 无法判断此题"}

    if confidence < threshold:
        with STATS_LOCK:
            STATS["low_conf"] += 1
        write_log({"t": int(time.time()), "title": clean, "type": qtype, "answer": answer,
                   "confidence": confidence, "low": True, "ms": el})
        return {"code": 0, "msg": f"AI 置信度低（{confidence:.2f}），建议人工核对：{answer}"}

    if use_cache:
        cache_put(key, clean, qtype, opts, answer, confidence, model)

    # AI 答出来的题顺手记进题库（标记为未核实），第二次作答可直接复用，且不怕选项乱序。
    # 之后如果抓到官方「正确答案」，会被 official 覆盖掉。
    route = CFG.get("route", {}) or {}
    if route.get("save_ai_answers", True):
        try:
            bank_put(clean, qtype, opts, answer, source="ai", verified=0)
        except Exception:
            pass

    write_log({"t": int(time.time()), "title": clean, "type": qtype, "options": opts,
               "answer": answer, "confidence": confidence, "model": model,
               "cached": False, "ms": el})
    return {"code": 1, "question": clean, "answer": answer, "ai": True,
            "source": "ai", "confidence": confidence, "cached": False,
            "model": model, "ms": el}


# ---------------------------------------------------------------- HTTP

def find_user_script(keyword=""):
    """在项目目录里找 .user.js 脚本。

    keyword 用于在多脚本共存时精确挑出要下发的那个（主脚本 / 题库助手）。
    """
    try:
        names = sorted(n for n in os.listdir(BASE_DIR) if n.endswith(".user.js"))
    except Exception:
        return None
    if not names:
        return None
    if keyword:
        for n in names:
            if keyword in n:
                return os.path.join(BASE_DIR, n)
        return None
    # 默认优先下发 OCS 主脚本，别把题库助手当成主脚本发出去
    for n in names:
        if "网课助手" in n:
            return os.path.join(BASE_DIR, n)
    return os.path.join(BASE_DIR, names[0])


# ---------------------------------------------------------------- 脚本版本快照 / 回退
VERSIONS_DIR = os.path.join(BASE_DIR, "backups")
VER_RE = re.compile(r"@version\s+([\d.]+)")


def script_version(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        m = VER_RE.search(head)
        return m.group(1) if m else "0.0.0"
    except Exception:
        return "0.0.0"


def snapshot_bank_script():
    """把当前题库助手留一份带版本号的快照，供随时回退。

    同一版本只留一份，不重复堆叠。返回快照文件名。
    """
    src = find_user_script("题库助手")
    if not src:
        return None
    ver = script_version(src)
    try:
        os.makedirs(VERSIONS_DIR, exist_ok=True)
    except Exception:
        return None
    dst = os.path.join(VERSIONS_DIR, f"OCS题库助手_v{ver}.user.js")
    try:
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
            log(f"已留档脚本快照：v{ver}")
        return os.path.basename(dst)
    except Exception:
        return None


def list_versions():
    """列出可回退的题库助手版本（含当前版本）。"""
    out = []
    cur = find_user_script("题库助手")
    cur_ver = script_version(cur) if cur else ""
    try:
        names = [n for n in os.listdir(VERSIONS_DIR) if n.endswith(".user.js")]
    except Exception:
        names = []
    for n in sorted(names):
        p = os.path.join(VERSIONS_DIR, n)
        v = script_version(p)
        try:
            st = os.stat(p)
        except Exception:
            continue
        out.append({"version": v, "file": n, "size": st.st_size,
                    "time": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
                    "current": v == cur_ver})
    out.sort(key=lambda x: [int(z) if z.isdigit() else 0 for z in x["version"].split(".")],
             reverse=True)
    return {"current": cur_ver, "versions": out}


def resolve_version_file(name):
    """只允许取 backups 目录下真实存在的 .user.js，防目录穿越。"""
    name = os.path.basename(str(name or "").strip())
    if not name.endswith(".user.js"):
        return None
    p = os.path.join(VERSIONS_DIR, name)
    if os.path.isfile(p):
        return p
    return None


INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OCS AI 题库 · 控制台</title>
<style>
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;max-width:720px;
margin:0 auto;padding:44px 24px 80px;color:#1a1a1a;line-height:1.65;background:#fafafa}
h1{font-size:22px;font-weight:600;margin:0 0 4px}
.sub{color:#666;font-size:13px;margin-bottom:26px}
.card{background:#fff;border:1px solid #e9e9e9;border-radius:12px;padding:18px 20px;margin-bottom:12px}
.step{display:flex;gap:14px;align-items:flex-start}
.num{flex:0 0 24px;height:24px;border-radius:50%;background:#1a1a1a;color:#fff;font-size:12px;
display:flex;align-items:center;justify-content:center;font-weight:600;margin-top:2px}
.ttl{font-size:15px;font-weight:600;margin-bottom:3px}
.desc{font-size:13px;color:#666;margin-bottom:12px}
button,.btn{display:inline-block;background:#1a1a1a;color:#fff;border:none;border-radius:8px;
padding:9px 18px;font-size:14px;cursor:pointer;text-decoration:none;font-family:inherit}
button:hover,.btn:hover{background:#333}
.ghost{background:#fff;color:#1a1a1a;border:1px solid #d9d9d9}
.ghost:hover{background:#f5f5f5}
code{background:#f1f1f1;padding:1px 5px;border-radius:4px;font-size:12px;
font-family:ui-monospace,Consolas,monospace}
table{width:100%;border-collapse:collapse;font-size:13px;margin-top:4px}
td{padding:6px 0;border-bottom:1px solid #f2f2f2;color:#666}
td:last-child{text-align:right;color:#1a1a1a;font-variant-numeric:tabular-nums}
.hint{font-size:12px;color:#999;margin-top:8px}
.warn{background:#fffbe6;border:1px solid #ffe58f;border-radius:8px;padding:11px 14px;
font-size:12.5px;color:#7a5c00;margin-top:12px;line-height:1.6}
.ok{color:#0f7b4f}
h2{font-size:14px;font-weight:600;margin:26px 0 8px;color:#333}
.modes{display:flex;gap:8px;flex-wrap:wrap}
.modes button{background:#fff;color:#1a1a1a;border:1px solid #d9d9d9}
.modes button.on{background:#1a1a1a;color:#fff;border-color:#1a1a1a}
textarea{width:100%;border:1px solid #d9d9d9;border-radius:8px;padding:10px;font-size:13px;
font-family:ui-monospace,Consolas,monospace;resize:vertical;line-height:1.55;color:#1a1a1a}
.banklist{max-height:340px;overflow:auto}
.bankitem{padding:9px 0;border-bottom:1px solid #f2f2f2;font-size:12.5px;line-height:1.55}
.bankitem .q{color:#333;word-break:break-all}
.bankitem .a{color:#0f7b4f;font-weight:600}
.tag{display:inline-block;font-size:11px;padding:1px 6px;border-radius:4px;background:#f1f1f1;
color:#666;margin-left:6px;font-weight:400}
.tag.v{background:#e7f6ee;color:#0f7b4f}
</style></head><body>
<h1>OCS AI 题库</h1>
<div class="sub">服务运行中 · 模型 <code>__MODEL__</code> · <span id="health" class="ok">检查中…</span></div>

<h2>三步开始</h2>

<div class="card"><div class="step"><div class="num">1</div><div style="flex:1">
<div class="ttl">安装脚本（两个都要装）</div>
<div class="desc">点按钮 → 脚本猫弹出安装页 → 点「安装」。第一个是 OCS 主脚本，负责答题；第二个是题库助手，负责抓答案和管理本地题库。</div>
<a class="btn" href="/install.user.js">安装 OCS 脚本</a>
<a class="btn ghost" href="/install.bank.user.js" style="margin-left:8px">安装题库助手</a>
</div></div></div>

<div class="card" id="verCard" style="display:none">
<div class="ttl">题库助手 · 历史版本（效果不对就装回旧的）</div>
<div class="desc" id="verDesc">加载中…</div>
<div id="verList" style="margin-top:10px"></div>
<div class="hint">点「装回这个版本」→ 脚本猫弹出安装页 → 点更新。题库数据不会动，只换脚本逻辑。</div>
</div>

<div class="card"><div class="step"><div class="num">2</div><div style="flex:1">
<div class="ttl">复制题库配置</div>
<div class="desc">复制后打开任意网课页面 → 点 OCS 悬浮球 → 题库配置 → 粘贴 → 保存。</div>
<button onclick="copyCfg(this)">一键复制题库配置</button>
</div></div></div>

<div class="card"><div class="step"><div class="num">3</div><div style="flex:1">
<div class="ttl">开始答题</div>
<div class="desc">打开章节测验或作业，先在左下角题库助手胶囊里选好路线，再点自动答题。</div>
<span class="hint">已经装过 OCS 普通版的，先去脚本猫里把它停用，两个同时开会互相打架。</span>
</div></div></div>

<div class="warn">面板里的「答题间隔」不要设成 0，全秒答是最容易被平台识别的特征。考试请自己答。</div>

<h2>答题路线</h2>
<div class="card">
<div class="desc" id="modeDesc">加载中…</div>
<div class="modes" id="modes"></div>
<div class="hint">切换立即生效，不用重启服务，也不需要改动 OCS 里的题库配置 —— 路由在本地服务这一层做。</div>
</div>

<h2>本地题库</h2>
<div class="card">
<table id="bankStats"><tr><td>加载中…</td><td></td></tr></table>
<div style="display:flex;gap:8px;margin-top:14px">
<button class="ghost" onclick="refreshBank()">刷新</button>
<button class="ghost" onclick="exportBank()">导出 JSON</button>
</div>
</div>

<div class="card">
<div class="ttl">从页面文本导入正确答案</div>
<div class="desc">把「查看答案 / 已批阅」页面全选复制（Ctrl+A、Ctrl+C），粘到下面。能识别「选项 + 正确答案：X」的结构，一次贴几十道都行。</div>
<textarea id="pasteBox" rows="9" placeholder="1. 【单选题】中国现存最早的完整农书是？&#10;A. 《齐民要术》&#10;B. 《农政全书》&#10;C. 《天工开物》&#10;正确答案：A&#10;&#10;2. 【判断题】……&#10;正确答案：正确"></textarea>
<div style="margin-top:10px"><button onclick="importText(this)">解析并入库</button></div>
<div class="hint" id="importMsg"></div>
</div>

<div class="card">
<div class="ttl">已有题目 <span class="tag" id="bankCount">0</span></div>
<div id="bankList" class="banklist"></div>
</div>

<h2>运行状态</h2>
<div class="sub" style="margin-bottom:10px">账户余额　<span id="balance">查询中…</span></div>
<table id="stats"><tr><td>加载中…</td><td></td></tr></table>

<script>
function copyCfg(btn){
  fetch('/ocs-config.json').then(r=>r.text()).then(t=>{
    const done=()=>{btn.textContent='已复制 ✓';setTimeout(()=>btn.textContent='一键复制题库配置',2000)};
    if(navigator.clipboard&&navigator.clipboard.writeText){
      navigator.clipboard.writeText(t).then(done).catch(()=>fallback(t,done));
    } else { fallback(t,done); }
  });
}
function fallback(t,done){
  const ta=document.createElement('textarea');ta.value=t;ta.style.position='fixed';ta.style.opacity='0';
  document.body.appendChild(ta);ta.select();
  try{document.execCommand('copy');done()}catch(e){alert('复制失败，请手动打开 ocs-题库配置.json')}
  document.body.removeChild(ta);
}
fetch('/health').then(r=>r.json()).then(d=>{
  document.getElementById('health').textContent = d.mock ? 'mock 模式（未接模型）' : '已连接模型';
});
fetch('/balance').then(r=>r.json()).then(d=>{
  const el=document.getElementById('balance');
  if(!el) return;
  el.textContent = d.ok ? ('¥'+d.total+'（充值 ¥'+d.topped_up+' / 赠送 ¥'+d.granted+'）') : (d.msg||'余额查询失败');
});
fetch('/stats').then(r=>r.json()).then(d=>{
  const s=d.stats||{},c=d.cache||{},u=d.usage||{},b=d.bank||{};
  const rows=[['总请求',s.total||0],
  ['本地题库命中',(s.bank_hit||0)+(b.hits?('（累计 '+(b.hits||0)+'）'):'')],
  ['题库未命中',s.bank_miss||0],
  ['AI 缓存命中',s.cache_hit||0],['调用模型',s.llm_call||0],
  ['调用失败',s.llm_fail||0],['低置信度跳过',s.low_conf||0],['平均耗时',(s.avg_ms||0)+' ms'],
  ['缓存题目数',c.entries||0],['题库题目数',b.entries||0],
  ['输入 tokens',u.prompt_tokens||0],['输出 tokens',u.completion_tokens||0],
  ['累计花费','¥'+(u.cost_cny||0).toFixed(4)],
  ['单题均价','¥'+(u.cost_per_question||0).toFixed(6)]];
  document.getElementById('stats').innerHTML=rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
});

// ---------- 路由模式
let MODES={},CUR='';
function loadRoute(){
  fetch('/route').then(r=>r.json()).then(d=>{
    MODES=d.modes||{};CUR=d.mode||'hybrid';
    renderModes();
  });
}
function renderModes(){
  const box=document.getElementById('modes');
  const label={hybrid:'本地优先 + AI 兜底',bank_only:'仅本地题库',ai_only:'仅 AI'};
  box.innerHTML=Object.keys(MODES).map(m=>
    '<button class="'+(m===CUR?'on':'')+'" onclick="setMode(\''+m+'\')">'+(label[m]||m)+'</button>'
  ).join('');
  document.getElementById('modeDesc').textContent=MODES[CUR]||'';
}
function setMode(m){
  fetch('/route',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:m})})
    .then(r=>r.json()).then(d=>{ if(d.ok){CUR=d.mode;renderModes();} else alert(d.msg||'切换失败'); });
}

// ---------- 题库
function refreshBank(){
  fetch('/bank?limit=300').then(r=>r.json()).then(d=>{
    const s=d.stats||{};
    const rows=[['题目总数',s.entries||0],['已核实答案',s.verified||0],
      ['被命中次数',s.hits||0],
      ['来源分布',Object.keys(s.by_source||{}).map(k=>k+':'+s.by_source[k]).join('  ')||'-']];
    document.getElementById('bankStats').innerHTML=
      rows.map(r=>'<tr><td>'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
    document.getElementById('bankCount').textContent=(s.entries||0)+' 题';
    const items=d.items||[];
    document.getElementById('bankList').innerHTML = items.length ? items.map(it=>
      '<div class="bankitem"><div class="q">'+esc(it.title)+
      '<span class="tag'+(it.verified?' v':'')+'">'+({official:'官方答案',manual:'手填',ai:'AI 存疑'}[it.source]||it.source)+'</span>'+
      (it.hits?'<span class="tag">命中 '+it.hits+'</span>':'')+'</div>'+
      '<div class="a">答案：'+esc(it.answer)+'</div></div>'
    ).join('') : '<div class="hint">题库还是空的。去「查看答案」页面抓取，或者把文本粘到上面那个框里。</div>';
  });
}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}

function importText(btn){
  const txt=document.getElementById('pasteBox').value;
  if(!txt.trim()){document.getElementById('importMsg').textContent='先粘点内容进来';return;}
  btn.disabled=true;btn.textContent='解析中…';
  fetch('/bank/import',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({text:txt})})
   .then(r=>r.json()).then(d=>{
     btn.disabled=false;btn.textContent='解析并入库';
     const msg=document.getElementById('importMsg');
     if(d.ok){
       msg.className='hint ok';
       msg.textContent='解析到 '+(d.parsed||0)+' 道题，入库 '+(d.saved||0)+' 道'+
         (d.skipped?('，跳过 '+(d.skipped||0)+' 道（没识别到正确答案）'):'');
       document.getElementById('pasteBox').value='';
       refreshBank();
     }else{
       msg.className='hint';msg.textContent='导入失败：'+(d.msg||'未知错误');
     }
   }).catch(e=>{btn.disabled=false;btn.textContent='解析并入库';
     document.getElementById('importMsg').textContent='请求失败：'+e;});
}
function exportBank(){
  fetch('/bank?limit=100000').then(r=>r.json()).then(d=>{
    const blob=new Blob([JSON.stringify(d.items||[],null,2)],{type:'application/json'});
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);
    a.download='OCS题库备份_'+new Date().toISOString().slice(0,10)+'.json';a.click();
  });
}

function loadVersions(){
  fetch('/versions').then(r=>r.json()).then(d=>{
    const card=document.getElementById('verCard');
    const list=d.versions||[];
    if(!list.length){ card.style.display='none'; return; }
    card.style.display='';
    document.getElementById('verDesc').textContent=
      '当前在跑：v'+(d.current||'?')+'　·　共 '+list.length+' 个留档版本。新版抓得不对，就装回上一个。';
    document.getElementById('verList').innerHTML=list.map(v=>
      '<div style="display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid #f2f2f2;font-size:12.5px">'
      +'<b style="min-width:52px">v'+v.version+'</b>'
      +'<span style="color:#888;flex:1">'+v.time+'　'+Math.round(v.size/1024)+' KB'
      +(v.current?'　<span class="tag v">当前</span>':'')+'</span>'
      +(v.current?'':'<a class="btn ghost" style="padding:5px 12px;font-size:12px" href="/install.bank.user.js?v='
        +encodeURIComponent(v.file)+'">装回这个版本</a>')
      +'</div>').join('');
  }).catch(()=>{});
}

loadRoute();refreshBank();loadVersions();
</script>
</body></html>
"""


TYPE_TAG_PARSE = re.compile(
    r"[【\[\(（]\s*(单选题?|多选题?|判断题?|填空题?|简答题?|论述题?|不定项选择题?)\s*[】\]\)）]")
OPT_LINE = re.compile(r"^\s*([A-Za-z])\s*[.．、\)）:：]\s*(.*)$")
ANS_LINE = re.compile(r"^\s*(?:正确答案|参考答案|标准答案|答案)\s*[:：]?\s*(.+)$")
SKIP_LINE = re.compile(
    r"^\s*(?:我的答案|得分|批语|评分|解析|答案解析|老师评语|试题解析)\s*[:：]")
NUM_LINE = re.compile(r"^\s*(\d{1,3})\s*[.、．\)）]\s*(.+)$")


def _infer_qtype(answer, options):
    """根据答案形态推断题型，用于把字母/文字答案规整成 OCS 能用的格式。"""
    a = str(answer or "").strip()
    if not a:
        return "unknown"
    if a in ("正确", "错误", "对", "错", "√", "×", "T", "F"):
        return "judgement"
    letters = plain_letters(a)
    if letters and options:
        return "single" if len(letters) == 1 else "multiple"
    if options and len(a) <= 4:
        return "single"
    return "completion"


def parse_bank_text(text):
    """把粘贴进来的「题目+选项+正确答案」纯文本解析成结构化题目列表。

    只保留能识别出正确答案的题目 —— 没答案的进了题库也没用，还会污染二次答题。
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = []
    cur = {"stem": [], "options": [], "answer": "", "type": ""}

    def close(b):
        stem = " ".join(x for x in b["stem"] if x).strip()
        ans = (b["answer"] or "").strip()
        if not stem or not ans:
            return
        qtype = normalize_qtype(b["type"]) if b["type"] else _infer_qtype(ans, b["options"])
        if qtype == "unknown":
            qtype = _infer_qtype(ans, b["options"])
        # 答案是一串字母、却连一个选项都没解析出来：这种条目无法在换顺序时对位，
        # 存进去只会添乱（本次那道金属探测器的题就是被这样拆坏的），直接丢弃。
        if re.fullmatch(r"[A-H](\s*#\s*[A-H])*", ans.upper()) and len(b["options"]) < 2:
            return
        fixed = normalize_answer(ans, qtype, b["options"])
        if not fixed:
            return
        blocks.append({"title": stem, "type": qtype,
                       "options": b["options"], "answer": fixed})

    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        m_ans = ANS_LINE.match(line)
        if m_ans:
            cur["answer"] = m_ans.group(1).strip()
            continue
        if SKIP_LINE.match(line):
            continue
        m_opt = OPT_LINE.match(line)
        # 已经拿到答案，又来了非选项行 → 上一题结束了
        if cur["answer"] and not m_opt:
            close(cur)
            cur = {"stem": [], "options": [], "answer": "", "type": ""}
            m_opt = OPT_LINE.match(line)
        if m_opt:
            cur["options"].append([m_opt.group(1).upper(), m_opt.group(2).strip()])
            continue
        m_type = TYPE_TAG_PARSE.search(line)
        if m_type and not cur["type"]:
            cur["type"] = m_type.group(1)
            line = TYPE_TAG_PARSE.sub("", line).strip()
        m_num = NUM_LINE.match(line)
        if m_num:
            line = m_num.group(2).strip()
        if line:
            cur["stem"].append(line)

    close(cur)
    return blocks


def save_config():
    """把内存里的 CFG 写回 config.json（切换路由模式后需要落盘）。"""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(CFG, f, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"配置写入失败: {e}")
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "OCSAI/1.1"
    def log_message(self, fmt, *args):
        pass  # 关掉默认逐请求日志，避免刷屏

    # ---------- 工具
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Private-Network", "true")
        self.end_headers()
        try:
            self.wfile.write(data)
        except Exception:
            pass

    def _auth_ok(self):
        token = str(CFG.get("server", {}).get("api_token", "") or "")
        if not token:
            return True
        return self.headers.get("X-Api-Token", "") == token

    def _params(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        p = {k: (v[0] if v else "") for k, v in qs.items()}
        if self.command == "POST":
            try:
                n = int(self.headers.get("Content-Length", "0") or 0)
                if 0 < n <= 2_000_000:
                    raw = self.rfile.read(n).decode("utf-8", errors="replace")
                    ctype = (self.headers.get("Content-Type") or "").lower()
                    body = None
                    if "json" in ctype or raw.lstrip().startswith("{") or raw.lstrip().startswith("["):
                        try:
                            body = json.loads(raw)
                        except Exception:
                            body = None
                    if body is None:
                        body = {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}
                    if isinstance(body, dict):
                        for k, v in body.items():
                            if isinstance(v, (list, dict)):
                                p[k] = json.dumps(v, ensure_ascii=False)
                            else:
                                p[k] = str(v)
            except Exception:
                pass
        return parsed.path.rstrip("/") or "/", p

    # ---------- 路由
    def do_OPTIONS(self):
        self._send(200, b"")

    def do_GET(self):
        path, p = self._params()
        if path == "/":
            html = INDEX_HTML.replace("__MODEL__", str(CFG.get("llm", {}).get("model", "-")))
            return self._send(200, html, "text/html; charset=utf-8")
        if path in ("/install.user.js", "/ocs.user.js", "/install.bank.user.js"):
            # 指定版本：/install.bank.user.js?v=OCS题库助手_v2.3.0.user.js
            want = (p.get("v") or "").strip()
            if want and path == "/install.bank.user.js":
                script = resolve_version_file(want)
                if not script:
                    return self._send(404, {"code": 0, "msg": "找不到这个历史版本：%s" % want})
            else:
                keyword = "题库助手" if path == "/install.bank.user.js" else "网课助手"
                script = find_user_script(keyword)
                if script and path == "/install.bank.user.js":
                    snapshot_bank_script()
            if not script:
                return self._send(404, {"code": 0, "msg": "脚本文件缺失：项目目录里没有 .user.js"})
            try:
                with open(script, "rb") as f:
                    data = f.read()
            except Exception as e:
                return self._send(500, {"code": 0, "msg": f"读取脚本失败：{e}"})
            log(f"下发脚本安装：{os.path.basename(script)}")
            return self._send(200, data, "text/javascript; charset=utf-8")
        if path == "/ocs-config.json":
            cfg_path = os.path.join(BASE_DIR, "ocs-题库配置.json")
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    txt = f.read()
            except Exception:
                txt = "[]"
            return self._send(200, txt, "application/json; charset=utf-8")
        if path == "/health":
            return self._send(200, {"status": "ok", "mock": MOCK,
                                    "model": CFG.get("llm", {}).get("model")})
        if path == "/versions":
            return self._send(200, list_versions())
        if path == "/balance":
            return self._send(200, deepseek_balance())
        if path == "/stats":
            with STATS_LOCK:
                s = dict(STATS)
                u = dict(USAGE)
            s["avg_ms"] = int(s["ms_sum"] / s["total"]) if s["total"] else 0
            price = CFG.get("pricing") or DEFAULT_PRICE
            miss = max(0, u["prompt_tokens"] - u["cached_tokens"])
            cost = (miss * float(price.get("input", 1.0))
                    + u["cached_tokens"] * float(price.get("cached_input", 0.02))
                    + u["completion_tokens"] * float(price.get("output", 2.0))) / 1_000_000
            u["cost_cny"] = round(cost, 4)
            u["cost_per_question"] = round(cost / s["total"], 6) if s["total"] else 0
            return self._send(200, {"stats": s, "cache": cache_stats(), "bank": bank_stats(),
                                    "usage": u, "route": get_route_mode(),
                                    "model": CFG.get("llm", {}).get("model"), "mock": MOCK})

        # ---------- 本地题库 & 路由
        if path == "/route":
            if self.command == "POST" and (p.get("mode") or "").strip():
                m = p.get("mode", "").strip().lower()
                if m not in ROUTE_MODES:
                    return self._send(400, {"ok": False,
                                            "msg": "mode 只能是 " + " / ".join(ROUTE_MODES)})
                CFG.setdefault("route", {})["mode"] = m
                save_config()
                log(f"路由模式已切换 → {m}（{MODE_DESC[m]}）")
                return self._send(200, {"ok": True, "mode": m})
            return self._send(200, {"mode": get_route_mode(), "modes": MODE_DESC})

        if path == "/bank":
            try:
                limit = max(1, min(100000, int(p.get("limit") or 200)))
                offset = max(0, int(p.get("offset") or 0))
            except Exception:
                limit, offset = 200, 0
            return self._send(200, {"mode": get_route_mode(), "modes": MODE_DESC,
                                    "stats": bank_stats(),
                                    "items": bank_list(limit, offset)})

        if path == "/bank/add":
            raw_items = []
            if p.get("items"):
                try:
                    parsed = json.loads(p["items"])
                    raw_items = parsed if isinstance(parsed, list) else [parsed]
                except Exception:
                    return self._send(400, {"ok": False, "msg": "items 不是合法 JSON"})
            elif p.get("title"):
                raw_items = [{"title": p.get("title"), "type": p.get("type"),
                              "options": p.get("options"), "answer": p.get("answer")}]
            if not raw_items:
                return self._send(400, {"ok": False, "msg": "没有收到题目"})

            saved = skipped = 0
            for it in raw_items:
                try:
                    title = clean_title(str(it.get("title") or ""))
                    ans_raw = str(it.get("answer") or "")
                    if not title or not ans_raw:
                        skipped += 1
                        continue
                    opts = it.get("options") or []
                    if isinstance(opts, str):
                        opts = parse_options(opts)
                    else:
                        norm_opts = []
                        for i, o in enumerate(opts):
                            if isinstance(o, (list, tuple)) and len(o) >= 2:
                                norm_opts.append([str(o[0]).upper(), str(o[1]).strip()])
                            else:
                                norm_opts.append([chr(65 + i), str(o).strip()])
                        opts = norm_opts
                    qtype = normalize_qtype(it.get("type") or "")
                    if qtype == "unknown":
                        qtype = _infer_qtype(ans_raw, opts)
                    answer = normalize_answer(ans_raw, qtype, opts)
                    if not answer:
                        skipped += 1
                        continue
                    src = str(it.get("source") or "official").lower()
                    if src not in BANK_SOURCE_RANK:
                        src = "official"
                    r = bank_put(title, qtype, opts, answer, source=src,
                                 verified=int(src != "ai"))
                    if r.get("ok"):
                        saved += 1
                    else:
                        skipped += 1
                except Exception as e:
                    log(f"入库单条失败: {e}")
                    skipped += 1
            log(f"题库入库：成功 {saved} 条，跳过 {skipped} 条")
            return self._send(200, {"ok": True, "saved": saved, "skipped": skipped})

        if path == "/bank/import":
            text = p.get("text") or ""
            if not text.strip():
                return self._send(400, {"ok": False, "msg": "文本为空"})
            dry = str(p.get("dry_run") or "").lower() in ("1", "true", "yes")
            items = parse_bank_text(text)
            if dry:
                # 只解析不入库，给抓取脚本做预览用
                return self._send(200, {"ok": True, "parsed": len(items),
                                        "items": items[:500]})
            saved = 0
            for it in items:
                r = bank_put(it["title"], it["type"], it["options"], it["answer"],
                             source="official", verified=1)
                if r.get("ok"):
                    saved += 1
            log(f"文本导入：解析 {len(items)} 道，入库 {saved} 道")
            return self._send(200, {"ok": True, "parsed": len(items), "saved": saved,
                                    "skipped": max(0, len(items) - saved)})

        if path == "/bank/delete":
            title = p.get("title") or ""
            if not title.strip():
                return self._send(400, {"ok": False, "msg": "缺少 title"})
            return self._send(200, bank_delete(title, p.get("type") or "unknown"))

        if path == "/bank/clear":
            conn = None
            try:
                with BANK_LOCK:
                    conn = bank_conn()
                    conn.execute("DELETE FROM bank")
                    conn.commit()
                log("题库已清空（服务端镜像）")
                return self._send(200, {"ok": True})
            except Exception as e:
                return self._send(500, {"ok": False, "msg": str(e)})
            finally:
                if conn:
                    conn.close()

        if path == "/bank/sync":
            # 浏览器题库是主库，这里按客户端为准全量替换服务端镜像
            try:
                parsed = json.loads(p.get("items") or "[]")
                if not isinstance(parsed, list):
                    return self._send(400, {"ok": False, "msg": "items 必须是数组"})
            except Exception:
                return self._send(400, {"ok": False, "msg": "items 不是合法 JSON"})

            # 先把整批校验成「确实能入库」的记录，再动数据库。
            # 之前是先 DELETE 再逐条 bank_put，遇到整批都被拒绝就会把库清空却写不进一条。
            prepared, rejected = [], 0
            for it in parsed:
                try:
                    title = clean_title(str(it.get("title") or ""))
                    ans_raw = str(it.get("answer") or "")
                    if not title or not ans_raw:
                        rejected += 1
                        continue
                    opts = it.get("options") or []
                    norm_opts = []
                    for i, o in enumerate(opts):
                        if isinstance(o, (list, tuple)) and len(o) >= 2:
                            norm_opts.append([str(o[0]).upper(), str(o[1]).strip()])
                        else:
                            norm_opts.append([chr(65 + i), str(o).strip()])
                    qtype = normalize_qtype(it.get("type") or "")
                    if qtype == "unknown":
                        qtype = _infer_qtype(ans_raw, norm_opts)
                    answer = normalize_answer(ans_raw, qtype, norm_opts)
                    if not answer:
                        rejected += 1
                        continue
                    if re.fullmatch(r"[A-H](\s*#\s*[A-H])*", str(answer).upper()) and len(norm_opts) < 2:
                        rejected += 1
                        continue
                    src = str(it.get("source") or "official").lower()
                    if src not in BANK_SOURCE_RANK:
                        src = "official"
                    prepared.append((title, qtype, norm_opts, answer, src))
                except Exception:
                    rejected += 1

            if parsed and not prepared:
                log(f"题库同步被拦下：客户端 {len(parsed)} 条全部不合格，服务端题库保持原样")
                return self._send(200, {"ok": False, "saved": 0, "received": len(parsed),
                                        "rejected": rejected,
                                        "msg": "整批不合格，已保留服务端原题库"})

            conn = None
            saved = 0
            try:
                with BANK_LOCK:
                    conn = bank_conn()
                    conn.execute("DELETE FROM bank")
                    conn.commit()
                for title, qtype, norm_opts, answer, src in prepared:
                    r = bank_put(title, qtype, norm_opts, answer, source=src,
                                 verified=int(src != "ai"))
                    if r.get("ok"):
                        saved += 1
                log(f"题库同步：客户端 {len(parsed)} 条，镜像 {saved} 条，跳过 {rejected} 条")
                return self._send(200, {"ok": True, "saved": saved,
                                        "received": len(parsed), "rejected": rejected})
            finally:
                if conn:
                    conn.close()

        if path in ("/query", "/api/search", "/search"):
            if not self._auth_ok():
                return self._send(403, {"code": 0, "msg": "invalid token"})
            title = p.get("title") or p.get("q") or p.get("question") or ""
            qtype = p.get("type") or p.get("qtype") or ""
            options = p.get("options") or ""
            return self._send(200, answer_question(title, qtype, options))
        return self._send(404, {"code": 0, "msg": "not found"})

    def do_POST(self):
        self.do_GET()


# ---------------------------------------------------------------- 入口


class AnswerServer(ThreadingHTTPServer):
    # Windows 下 allow_reuse_address=True 会让两个进程同时绑同一端口而不报错，
    # 结果是双击两次就起了两个服务、各自记账各自缓存。这里关掉，让占用能被发现。
    allow_reuse_address = False
    daemon_threads = True


def main():
    global CFG, MOCK, SEM
    setup_stdout()
    ap = argparse.ArgumentParser(description="OCS AI 题库服务")
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--port", type=int)
    ap.add_argument("--host")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--clear-cache", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    if args.clear_cache:
        conn = db_conn()
        conn.execute("DELETE FROM answers")
        conn.commit()
        conn.close()
        print("缓存已清空")
        return

    if args.stats:
        print(json.dumps({"cache": cache_stats()}, ensure_ascii=False, indent=2))
        return

    try:
        CFG = load_config(args.config)
    except FileNotFoundError:
        print(f"找不到配置文件：{args.config}")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"config.json 格式错误：{e}")
        sys.exit(1)

    key = str(CFG.get("llm", {}).get("api_key", "")).strip()
    if args.mock:
        MOCK = True
    elif not key or key.startswith("sk-在这里") or "填" in key:
        MOCK = True
        log("未配置 api_key，已进入 mock 模式（只验证链路，不调用模型）")

    SEM = threading.Semaphore(int(CFG.get("llm", {}).get("max_concurrency", 4)))

    # 启动时给题库助手留一份带版本号的快照，方便随时回退
    try:
        snapshot_bank_script()
    except Exception:
        pass

    srv = CFG.get("server", {})
    host = args.host or srv.get("host", "127.0.0.1")
    port = args.port or int(srv.get("port", 8321))

    try:
        httpd = AnswerServer((host, port), Handler)
    except OSError as e:
        if getattr(e, "errno", None) in (48, 98, 10048) or "in use" in str(e).lower():
            log("-" * 52)
            log(f"端口 {port} 已被占用：说明服务已经在运行了。")
            log(f"直接打开 http://{host}:{port} 即可，不用再启动一个。")
            log("如果打不开，说明占用端口的是别的程序，改 config.json 里的 port。")
            log("-" * 52)
        else:
            log(f"启动失败：{e}")
        try:
            input("按回车关闭窗口...")
        except Exception:
            pass
        return
    httpd.daemon_threads = True

    if not args.no_browser and host in ("127.0.0.1", "localhost") and srv.get("open_browser", True):
        import webbrowser
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    log("=" * 52)
    log(f"OCS AI 题库服务已启动  http://{host}:{port}")
    log(f"模型：{CFG.get('llm', {}).get('model')}  {'（mock 模式）' if MOCK else ''}")
    log(f"答题路线：{get_route_mode()} —— {MODE_DESC[get_route_mode()]}")
    log(f"本地题库：{bank_stats().get('entries', 0)} 题")
    log(f"置信度阈值：{CFG.get('behavior', {}).get('confidence_threshold')}   缓存：{CFG.get('behavior', {}).get('cache_enabled')}")
    log("把 ocs-题库配置.json 粘贴进 OCS 面板的「题库配置」")
    log("按 Ctrl+C 停止")
    log("=" * 52)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("已停止")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
