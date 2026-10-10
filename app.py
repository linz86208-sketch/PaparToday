
from flask import Flask, jsonify, render_template
import requests
import time
from datetime import datetime

app = Flask(__name__)

# 期刊名称与 ISSN
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

# 缓存论文数据，减少重复请求
CACHE = {}
CACHE_SECONDS = 1800


def get_publish_date(item):
    """提取论文发表日期"""
    info = (
        item.get("published-online")
        or item.get("published-print")
        or item.get("published")
        or {}
    )

    parts = info.get("date-parts", [[]])[0]

    if not parts:
        return "未知日期"

    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1

    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return "未知日期"


def fetch_journal_papers(journal_key):
    """从 Crossref 获取某个期刊的最新论文"""
    journal = JOURNALS[journal_key]

    # 优先使用缓存
    now = time.time()

    if journal_key in CACHE:
        cached_time, cached_data = CACHE[journal_key]
        if now - cached_time < CACHE_SECONDS:
            return cached_data

    url = (
        f"https://api.crossref.org/journals/"
        f"{journal['issn']}/works"
    )

    params = {
        "filter": "type:journal-article",
        "sort": "published",
        "order": "desc",
        "rows": 20
    }

    response = requests.get(
        url,
        params=params,
        headers={
            "User-Agent": "PaperToday/0.1 (academic project)"
        },
        timeout=20
    )

    response.raise_for_status()

    items = response.json()["message"]["items"]

    papers = []

    for item in items:
        title = item.get("title", [""])

        if not title or not title[0].strip():
            continue

        authors = []

        for author in item.get("author", [])[:4]:
            name = (
                author.get("given", "") + " " +
                author.get("family", "")
            ).strip()

            if name:
                authors.append(name)

        doi = item.get("DOI", "")

        paper = {
            "title": title[0],
            "authors": ", ".join(authors) if authors else "未知作者",
            "journal": journal["name"],
            "date": get_publish_date(item),
            "doi": doi,
            "url": f"https://doi.org/{doi}" if doi else item.get("URL", "")
        }

        papers.append(paper)

    CACHE[journal_key] = (now, papers)
    return papers


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/api/papers")
def get_papers():
    from flask import request

    journal = request.args.get("journal", "all").lower()

    if journal != "all" and journal not in JOURNALS:
        return jsonify({
            "error": "不支持的期刊分类"
        }), 400

    keys = list(JOURNALS.keys()) if journal == "all" else [journal]

    all_papers = []
    errors = []

    for key in keys:
        try:
            all_papers.extend(fetch_journal_papers(key))
        except (requests.RequestException, ValueError, KeyError) as e:
            app.logger.warning("获取 %s 失败: %s", key, e)
            errors.append(key)

    # 论文按照发表日期排序
    all_papers.sort(
        key=lambda x: x["date"],
        reverse=True
    )

    return jsonify({
        "papers": all_papers,
        "count": len(all_papers),
        "errors": errors
    })


if __name__ == "__main__":
    app.run(debug=True)
