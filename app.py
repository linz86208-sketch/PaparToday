
from flask import Flask, jsonify, render_template, request
import requests
import time
import os
import json
import logging
import re
from html import unescape
from datetime import datetime
from threading import Lock

# ==========================================
# 1. Flask 初始化
# ==========================================

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)


# ==========================================
# 2. 学术期刊配置
# ==========================================

JOURNALS = {
    "nature": {
        "name": "Nature",
        "issn": "0028-0836"
    },
    "science": {
        "name": "Science",
        "issn": "0036-8075"
    },
    "cell": {
        "name": "Cell",
        "issn": "0092-8674"
    }
}


# ==========================================
# 3. 缓存配置
# ==========================================

# 论文缓存
PAPER_CACHE = {}

# 论文缓存有效期：30 分钟
CACHE_SECONDS = 1800

# 翻译缓存
TRANSLATION_CACHE = {}

# 单篇论文的翻译与总结缓存
ENRICHMENT_CACHE = {}

# 线程锁
cache_lock = Lock()


# ==========================================
# 4. DeepSeek API 配置
# ==========================================

DEEPSEEK_API_KEY = os.environ.get(
    "DEEPSEEK_API_KEY", ""
)

DEEPSEEK_API_URL = (
    "https://api.deepseek.com/chat/completions"
)

DEEPSEEK_MODEL = os.environ.get(
    "DEEPSEEK_MODEL",
    "deepseek-flash"
)


# ==========================================
# 5. 论文日期处理
# ==========================================

def get_publish_date(item):
    """
    从 Crossref 元数据提取发表日期
    """

    info = (
        item.get("published-online")
        or item.get("published-print")
        or item.get("published")
        or {}
    )

    date_parts = info.get("date-parts") or [[]]
    parts = date_parts[0]

    if not parts:
        return "未知日期"

    try:
        year = int(parts[0])
        month = int(parts[1]) if len(parts) > 1 else 1
        day = int(parts[2]) if len(parts) > 2 else 1

        return datetime(
            year, month, day
        ).strftime("%Y-%m-%d")

    except (ValueError, TypeError, OverflowError):
        return "未知日期"


def clean_abstract(value):
    """清理 Crossref 摘要中常见的 JATS/HTML 标签。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


# ==========================================
# 6. 获取期刊论文
# ==========================================

def fetch_journal_papers(journal_key):
    """
    调用 Crossref API 获取指定期刊论文
    """

    journal = JOURNALS[journal_key]

    now = time.time()

    # 优先读取缓存
    with cache_lock:
        cached = PAPER_CACHE.get(journal_key)

        if cached:
            cached_time, cached_data = cached

            if now - cached_time < CACHE_SECONDS:
                return cached_data

    url = (
        "https://api.crossref.org/journals/"
        f"{journal['issn']}/works"
    )

    params = {
        "filter": "type:journal-article",
        "sort": "published",
        "order": "desc",
        "rows": 20
    }

    headers = {
        "User-Agent": (
            "PaperToday/1.0 "
            "(Academic Research News Aggregator)"
        ),
        "Accept": "application/json"
    }

    response = requests.get(
        url,
        params=params,
        headers=headers,
        timeout=20
    )

    response.raise_for_status()

    items = response.json()["message"]["items"]

    papers = []

    for item in items:

        titles = item.get("title") or []

        if not titles:
            continue

        title = titles[0].strip()

        if not title:
            continue

        # 作者信息
        authors = []

        for author in item.get("author", [])[:4]:

            given = author.get("given", "")
            family = author.get("family", "")

            name = f"{given} {family}".strip()

            if name:
                authors.append(name)

        author_text = (
            ", ".join(authors)
            if authors
            else "未知作者"
        )

        # DOI 信息
        doi = item.get("DOI", "")

        if doi:
            paper_url = f"https://doi.org/{doi}"
        else:
            paper_url = item.get("URL", "")

        paper = {
            "title": title,
            "authors": author_text,
            "journal": journal["name"],
            "date": get_publish_date(item),
            "doi": doi,
            "url": paper_url,
            "abstract": clean_abstract(item.get("abstract", ""))
        }

        papers.append(paper)

    # 保存缓存
    with cache_lock:
        PAPER_CACHE[journal_key] = (
            time.time(),
            papers
        )

    app.logger.info(
        "成功获取 %s 论文 %s 篇",
        journal["name"],
        len(papers)
    )

    return papers


# ==========================================
# 7. 网站首页
# ==========================================

@app.route("/")
def home():
    return render_template("index.html")


# ==========================================
# 8. 论文数据接口
# ==========================================

@app.route("/api/papers", methods=["GET"])
def get_papers():

    journal = request.args.get(
        "journal", "all"
    ).lower()

    if (
        journal != "all"
        and journal not in JOURNALS
    ):
        return jsonify({
            "error": "不支持的期刊分类"
        }), 400

    if journal == "all":
        keys = list(JOURNALS.keys())
    else:
        keys = [journal]

    all_papers = []
    errors = []

    for key in keys:

        try:
            papers = fetch_journal_papers(key)

            all_papers.extend(papers)

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError
        ) as error:

            app.logger.warning(
                "获取 %s 论文失败: %s",
                key,
                error
            )

            errors.append(key)

            # 数据源故障时尝试读取旧缓存
            with cache_lock:
                cached = PAPER_CACHE.get(key)

            if cached:
                all_papers.extend(cached[1])

    # 按发表日期排序
    all_papers.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    return jsonify({
        "papers": all_papers,
        "count": len(all_papers),
        "errors": errors
    })


def call_deepseek_enrich(paper):
    """翻译当前论文，并根据可用元数据生成简洁中文总结。"""
    if not DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek API Key 未配置")

    source_abstract = paper.get("abstract", "").strip()
    prompt = {
        "title": paper.get("title", ""),
        "abstract": source_abstract,
        "journal": paper.get("journal", ""),
        "authors": paper.get("authors", "")
    }
    system_prompt = """
你是一名严谨的科研编辑。请只依据用户提供的论文元数据完成中文翻译与总结。
返回合法 JSON 对象，必须包含以下字符串字段：
title_zh：论文标题的简体中文翻译；
abstract_zh：摘要的完整简体中文翻译；如果没有摘要，明确写“该论文暂未提供公开摘要”；
summary_zh：用通俗但准确的中文写 3 至 5 段概述，说明研究主题、可能的方法、主要信息和意义。
不得编造用户没有提供的实验数据、结论或研究方法。没有摘要时，只能根据标题说明研究主题，并明确提示信息有限。
"""
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)}
        ],
        "response_format": {"type": "json_object"},
        "stream": False,
        "max_tokens": 3500
    }
    response = requests.post(
        DEEPSEEK_API_URL,
        headers={
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=90
    )
    response.raise_for_status()
    result = response.json()
    choices = result.get("choices") or []
    if not choices:
        raise ValueError("DeepSeek 返回内容为空")
    parsed = json.loads(choices[0]["message"]["content"])
    required = ("title_zh", "abstract_zh", "summary_zh")
    if any(not isinstance(parsed.get(key), str) or not parsed[key].strip()
           for key in required):
        raise ValueError("DeepSeek 返回字段不完整")
    return {key: parsed[key].strip() for key in required}


@app.route("/api/enrich", methods=["POST"])
def enrich_paper():
    """处理当前展示的单篇论文，避免一次翻译整批论文。"""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "请求必须是 JSON 对象"}), 400

    title = data.get("title", "")
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        return jsonify({"error": "论文标题无效"}), 400

    paper = {
        "title": title.strip(),
        "abstract": data.get("abstract", "") if isinstance(data.get("abstract", ""), str) else "",
        "journal": data.get("journal", "") if isinstance(data.get("journal", ""), str) else "",
        "authors": data.get("authors", "") if isinstance(data.get("authors", ""), str) else ""
    }
    paper["abstract"] = paper["abstract"][:12000]
    cache_key = paper["title"] + "\n" + paper["abstract"]

    with cache_lock:
        cached = ENRICHMENT_CACHE.get(cache_key)
    if cached:
        return jsonify(cached)
    if not DEEPSEEK_API_KEY:
        return jsonify({"error": "DeepSeek API 尚未配置"}), 503

    try:
        enriched = call_deepseek_enrich(paper)
    except (requests.RequestException, ValueError, KeyError, TypeError,
            RuntimeError, json.JSONDecodeError) as error:
        app.logger.warning("论文翻译与总结失败: %s", error)
        return jsonify({"error": "翻译与总结服务暂时不可用"}), 502

    with cache_lock:
        ENRICHMENT_CACHE[cache_key] = enriched
    return jsonify(enriched)


# ==========================================
# 9. DeepSeek 学术翻译
# ==========================================

def call_deepseek_translate(titles):
    """
    调用 DeepSeek 翻译英文论文标题

    参数：
        titles: 英文标题列表

    返回：
        中文标题列表
    """

    if not DEEPSEEK_API_KEY:
        raise RuntimeError(
            "DeepSeek API Key 未配置"
        )

    system_prompt = """
你是一名专业的科研论文翻译助手。

任务：
将用户提供的英文学术论文标题翻译为简体中文。

要求：
1. 翻译准确，符合中文学术表达习惯。
2. 正确保留专业术语、人名与缩写。
3. 不要自行增加原文不存在的信息。
4. 不要解释论文内容。
5. 翻译顺序必须与输入顺序完全一致。
6. 只返回合法 JSON 对象。

JSON 输出格式示例：

{
  "translations": [
    "第一篇论文中文标题",
    "第二篇论文中文标题"
  ]
}
"""

    user_prompt = json.dumps(
        {
            "titles": titles
        },
        ensure_ascii=False
    )

    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {
                "role": "system",
                "content": system_prompt
            },
            {
                "role": "user",
                "content": user_prompt
            }
        ],
        "response_format": {
            "type": "json_object"
        },
        "stream": False,
        "max_tokens": 3000
    }

    headers = {
        "Authorization": (
            f"Bearer {DEEPSEEK_API_KEY}"
        ),
        "Content-Type": "application/json"
    }

    response = requests.post(
        DEEPSEEK_API_URL,
        headers=headers,
        json=payload,
        timeout=60
    )

    response.raise_for_status()

    result = response.json()

    choices = result.get("choices", [])

    if not choices:
        raise ValueError(
            "DeepSeek 返回内容为空"
        )

    choice = choices[0]

    if choice.get("finish_reason") != "stop":
        raise ValueError(
            "DeepSeek 未正常完成翻译"
        )

    content = choice["message"]["content"]

    if not content:
        raise ValueError(
            "DeepSeek 翻译结果为空"
        )

    parsed = json.loads(content)

    translated = parsed.get("translations")

    if not isinstance(translated, list):
        raise ValueError(
            "翻译结果不是数组"
        )

    if len(translated) != len(titles):
        raise ValueError(
            "翻译结果数量不匹配"
        )

    for item in translated:

        if (
            not isinstance(item, str)
            or not item.strip()
        ):
            raise ValueError(
                "翻译结果包含无效标题"
            )

    return translated


# ==========================================
# 10. 论文自动翻译接口
# ==========================================

@app.route("/api/translate", methods=["POST"])
def translate_papers():

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({
            "error": "请求必须是 JSON 对象"
        }), 400

    titles = data.get("titles", [])

    # 参数类型检查
    if not isinstance(titles, list):
        return jsonify({
            "error": "titles 必须为数组"
        }), 400

    # 限制批量翻译规模
    if len(titles) > 12:
        return jsonify({
            "error": "单次最多翻译 12 篇论文"
        }), 400

    for title in titles:

        if not isinstance(title, str):
            return jsonify({
                "error": "论文标题必须为字符串"
            }), 400

        if len(title) > 500:
            return jsonify({
                "error": "论文标题长度超限"
            }), 400

    if not titles:
        return jsonify({
            "translations": []
        })

    if not DEEPSEEK_API_KEY:
        return jsonify({
            "error": "DeepSeek API 尚未配置"
        }), 503

    # 收集未翻译的标题
    with cache_lock:
        missing = list(dict.fromkeys(
            title
            for title in titles
            if (
                title.strip()
                and title not in TRANSLATION_CACHE
            )
        ))

    if missing:

        try:

            translated = call_deepseek_translate(
                missing
            )

            with cache_lock:
                for original, chinese in zip(
                    missing,
                    translated
                ):
                    TRANSLATION_CACHE[original] = chinese

            app.logger.info(
                "成功翻译 %s 篇论文",
                len(missing)
            )

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError,
            RuntimeError,
            json.JSONDecodeError
        ) as error:

            app.logger.warning(
                "DeepSeek 翻译失败: %s",
                error
            )

            return jsonify({
                "error": "翻译服务暂时不可用"
            }), 502

    # 按前端提交的顺序返回译文
    with cache_lock:
        result = [
            TRANSLATION_CACHE.get(
                title,
                title
            )
            for title in titles
        ]

    return jsonify({
        "translations": result
    })


# ==========================================
# 11. 健康检查接口
# ==========================================

@app.route("/health", methods=["GET"])
def health_check():

    return jsonify({
        "status": "ok",
        "service": "PaperToday",
        "translation_configured": bool(
            DEEPSEEK_API_KEY
        )
    })


# ==========================================
# 12. 本地开发启动
# ==========================================

if __name__ == "__main__":

    app.run(
        host="127.0.0.1",
        port=5000,
        debug=True
    )
