
from flask import Flask, jsonify, render_template, request
import requests
import time
import os
import json
import logging
import re
import xml.etree.ElementTree as ET
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

# Crossref 单次候选数量与最终展示数量
CROSSREF_ROWS = 40
TARGET_PAPERS = 20

# 少于该长度的摘要通常是新闻、评论或期刊导读，不足以生成具体导读
MIN_ABSTRACT_LENGTH = 300

# 用于识别生物医学论文的主题词
BIOLOGY_TERMS = (
    "gene", "genes", "genetic", "genome", "genomic", "dna", "rna",
    "protein", "proteins", "proteome", "transcriptome", "chromatin",
    "chromosome", "enzyme", "receptor", "antibody", "immune", "cancer",
    "tumor", "tumour", "neuron", "brain", "synapse", "axon",
    "dendritic", "cell", "cellular", "stem cell", "embryo", "tissue",
    "organism", "plant", "animal", "bacteria", "bacterial", "virus",
    "viral", "microbiome", "pathogen", "infection", "vaccine",
    "disease", "clinical", "patient", "physiology", "metabolism",
    "metabolic", "mitochondria", "chloroplast", "photosynthesis",
    "crispr", "splicing", "spliceosome", "ribosome", "peptide",
    "hormone", "cytokine", "malaria", "parasite", "fungus", "fungal",
    "antibiotic", "lung", "liver", "kidney", "blood", "hemoglobin",
    "haemoglobin", "muscle", "bone", "skin", "epidermal", "epithelial",
    "intestine", "intestinal", "gut", "tetrapod", "fossil",
    "butterfly", "mangrove", "mammal", "primate", "mouse", "mice",
    "zebrafish", "drosophila", "arabidopsis", "yeast", "biopolymer",
    "ciliary", "autism", "myeloid", "tyk2", "amygdala",
    "hypothalamic", "redox", "alkaloid", "bird", "insect", "wing",
    "flight"
)

# 这些主题词出现在标题中时，优先判定为非生物医学论文
NON_BIOLOGY_TERMS = (
    "quantum", "semiconductor", "semiconductors", "alloy", "alloys",
    "mosfet", "superconductor", "superconducting", "plasmonic", "photonic",
    "laser", "optical clock", "nuclear clock", "thorium", "seaborgium",
    "carbene", "catalysis", "catalyst", "materials", "crystal", "crystals",
    "ferroelectric", "magnetism", "magnetic", "spintronic", "moiré", "moire",
    "graphene", "battery", "climate", "enso", "glacier", "ice cliff",
    "geophysics", "astrophysics", "cosmology", "galaxy", "neutrino",
    "gravitational wave", "black hole", "dark matter", "particle physics",
    "cryptography", "microelectronics", "photovoltaics",
    "thermoelectric", "metamaterial", "optics", "spectroscopy", "plasma",
    "fusion", "reactor"
)

BIOLOGY_PATTERN = re.compile(
    r"\b(?:" + "|".join(map(re.escape, BIOLOGY_TERMS)) + r")\b",
    re.IGNORECASE
)

NON_BIOLOGY_PATTERN = re.compile(
    r"\b(?:" + "|".join(map(re.escape, NON_BIOLOGY_TERMS)) + r")\b",
    re.IGNORECASE
)

AI_PATTERN = re.compile(
    r"\b(?:artificial intelligence|machine learning|deep learning|"
    r"language models?|large language models?|llms?|foundation models?|"
    r"generative models?|diffusion models?|reinforcement learning|"
    r"self-supervised learning|neural networks?|transformers?|"
    r"computer vision|natural language processing|ai-generated|ai-driven|"
    r"ai models?)\b",
    re.IGNORECASE
)

# 这些 PubMed 文献类型不是原创研究论文，不进入首页
NON_RESEARCH_PUBLICATION_TYPES = {
    "biography",
    "comment",
    "editorial",
    "interview",
    "letter",
    "news",
    "obituary",
    "published erratum",
    "retracted publication",
    "retraction of publication"
}

PUBMED_TYPE_CACHE = {}

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

OPENALEX_API_URL = "https://api.openalex.org/works"

PUBMED_ESEARCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
)

PUBMED_EFETCH_URL = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
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


def clean_text(value):
    """清理 Crossref 元数据中常见的 JATS/HTML 标签。"""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def clean_abstract(value):
    """清理 Crossref 摘要。"""
    return clean_text(value)


def normalize_doi(value):
    """统一 DOI 格式，便于跨数据源匹配。"""
    if not isinstance(value, str):
        return ""
    doi = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        value.strip(),
        flags=re.IGNORECASE
    )
    return doi.lower()


def abstract_from_inverted_index(index):
    """将 OpenAlex 的摘要倒排索引还原为正文。"""
    if not isinstance(index, dict):
        return ""

    positions = []

    for word, locations in index.items():

        if not isinstance(word, str) or not isinstance(locations, list):
            continue

        for position in locations:

            if isinstance(position, int):
                positions.append((position, word))

    if not positions:
        return ""

    positions.sort(key=lambda pair: pair[0])
    text = " ".join(word for _, word in positions)

    # 倒排索引把标点也作为独立词元，拼接后需要去掉多余空格
    text = re.sub(r"\s+([,.;:!?%)\]])", r"\1", text)
    text = re.sub(r"([(\[])\s+", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_openalex_abstracts(papers):
    """用 OpenAlex 批量补齐 Crossref 缺失的摘要。"""
    missing = [
        paper for paper in papers
        if paper.get("doi") and not paper.get("abstract")
    ]

    if not missing:
        return

    for start in range(0, len(missing), 20):

        batch = missing[start:start + 20]
        filter_value = "doi:" + "|".join(
            paper["doi"] for paper in batch
        )

        try:
            response = requests.get(
                OPENALEX_API_URL,
                params={
                    "filter": filter_value,
                    "per-page": 50,
                    "select": "doi,abstract_inverted_index"
                },
                headers={
                    "User-Agent": "PaperToday/1.0",
                    "Accept": "application/json"
                },
                timeout=20
            )

            response.raise_for_status()
            works = response.json().get("results") or []

        except (
            requests.RequestException,
            ValueError,
            TypeError
        ) as error:

            app.logger.warning(
                "OpenAlex 摘要回填失败: %s",
                error
            )
            return

        abstract_by_doi = {}

        for work in works:

            doi = normalize_doi(work.get("doi"))
            abstract = abstract_from_inverted_index(
                work.get("abstract_inverted_index")
            )

            if doi and abstract:
                abstract_by_doi[doi] = abstract

        for paper in batch:

            paper["abstract"] = abstract_by_doi.get(
                normalize_doi(paper["doi"]),
                ""
            )


def fetch_pubmed_types(dois):
    """批量查询 PubMed 文献类型，查询失败时返回已有缓存。"""
    now = time.time()
    result = {}
    missing = []

    with cache_lock:

        for doi in dois:

            cached = PUBMED_TYPE_CACHE.get(doi)

            if cached and now - cached[0] < CACHE_SECONDS:
                result[doi] = cached[1]
            else:
                missing.append(doi)

    headers = {
        "User-Agent": "PaperToday/1.0",
        "Accept": "application/json"
    }

    for start in range(0, len(missing), 10):

        batch = missing[start:start + 10]
        query = " OR ".join(
            f'"{doi}"[doi]' for doi in batch
        )

        try:
            response = requests.get(
                PUBMED_ESEARCH_URL,
                params={
                    "db": "pubmed",
                    "term": query,
                    "retmode": "json"
                },
                headers=headers,
                timeout=20
            )

            response.raise_for_status()
            pmids = response.json()["esearchresult"]["idlist"]

        except (
            requests.RequestException,
            ValueError,
            KeyError,
            TypeError
        ) as error:

            app.logger.warning(
                "PubMed 类型查询失败: %s",
                error
            )
            break

        if not pmids:
            for doi in batch:
                result[doi] = []
            continue

        time.sleep(0.34)

        try:
            response = requests.get(
                PUBMED_EFETCH_URL,
                params={
                    "db": "pubmed",
                    "id": ",".join(pmids),
                    "retmode": "xml"
                },
                headers=headers,
                timeout=20
            )

            response.raise_for_status()
            root = ET.fromstring(response.content)

        except (
            requests.RequestException,
            ET.ParseError
        ) as error:

            app.logger.warning(
                "PubMed 记录解析失败: %s",
                error
            )
            break

        for article in root.findall(".//PubmedArticle"):

            doi = ""

            for element in article.findall(".//ELocationID"):

                if element.attrib.get("EIdType") == "doi":
                    doi = normalize_doi(element.text)
                    break

            if not doi:
                continue

            result[doi] = [
                (element.text or "").strip().lower()
                for element in article.findall(
                    ".//PublicationTypeList/PublicationType"
                )
            ]

        for doi in batch:
            result.setdefault(doi, [])

        time.sleep(0.34)

    with cache_lock:

        for doi, types in result.items():
            PUBMED_TYPE_CACHE[doi] = (now, types)

    return result


def is_target_paper(paper):
    """根据期刊和主题词判断论文是否属于生物医学或 AI 方向。"""
    if paper.get("journal") == "Cell":
        return True

    title = paper.get("title", "")
    text = title + " " + paper.get("abstract", "")

    title_biology = {
        match.group(0).lower()
        for match in BIOLOGY_PATTERN.finditer(title)
    }

    title_ai = {
        match.group(0).lower()
        for match in AI_PATTERN.finditer(title)
    }

    if title_ai:
        return True

    if NON_BIOLOGY_PATTERN.search(title) and not title_biology:
        return False

    if title_biology:
        return True

    abstract_ai = {
        match.group(0).lower()
        for match in AI_PATTERN.finditer(text)
    }

    if len(abstract_ai) >= 2:
        return True

    abstract_biology = {
        match.group(0).lower()
        for match in BIOLOGY_PATTERN.finditer(text)
    }

    return len(abstract_biology) >= 2


def filter_research_papers(papers):
    """排除评论、新闻、社论，以及非生物医学或 AI 方向的研究记录。"""
    dois = list(dict.fromkeys(
        normalize_doi(paper.get("doi"))
        for paper in papers
        if paper.get("doi")
    ))

    if not dois:
        return papers

    types_by_doi = fetch_pubmed_types(dois)
    research_papers = []

    for paper in papers:

        types = set(types_by_doi.get(
            normalize_doi(paper.get("doi")),
            []
        ))

        if types & NON_RESEARCH_PUBLICATION_TYPES:
            continue

        if not is_target_paper(paper):
            continue

        research_papers.append(paper)

    return research_papers


# ==========================================
# 6. 获取期刊论文
# ==========================================

def fetch_crossref_items(journal, require_abstract):
    """从 Crossref 获取候选论文元数据。"""
    url = (
        "https://api.crossref.org/journals/"
        f"{journal['issn']}/works"
    )

    filter_value = "type:journal-article"

    if require_abstract:
        filter_value += ",has-abstract:true"

    params = {
        "filter": filter_value,
        "sort": "published",
        "order": "desc",
        "rows": CROSSREF_ROWS
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

    return response.json()["message"]["items"]


def build_paper(item, journal):
    """把一条 Crossref 记录转换为前端使用的论文对象。"""
    titles = item.get("title") or []

    if not titles:
        return None

    title = clean_text(titles[0])

    if not title:
        return None

    authors = []

    for author in item.get("author", [])[:4]:

        given = clean_text(author.get("given", ""))
        family = clean_text(author.get("family", ""))

        name = f"{given} {family}".strip()

        if name:
            authors.append(name)

    author_text = (
        ", ".join(authors)
        if authors
        else "未知作者"
    )

    doi = (item.get("DOI") or "").strip()

    if doi:
        paper_url = f"https://doi.org/{doi}"
    else:
        paper_url = item.get("URL", "")

    return {
        "title": title,
        "authors": author_text,
        "journal": journal["name"],
        "date": get_publish_date(item),
        "doi": doi,
        "url": paper_url,
        "abstract": clean_abstract(item.get("abstract", ""))
    }


def deduplicate_papers(papers):
    """按 DOI 或标题去重，保持原始顺序。"""
    unique = []
    seen = set()

    for paper in papers:

        key = (
            normalize_doi(paper.get("doi"))
            or paper.get("title", "").strip().lower()
        )

        if not key or key in seen:
            continue

        seen.add(key)
        unique.append(paper)

    return unique


def fetch_journal_papers(journal_key):
    """
    获取指定期刊中有实际摘要的论文。

    Nature/Science 的 Crossref 记录中混有大量无摘要的新闻与社论，
    因此优先请求 has-abstract 记录，并过滤只有一句话的短讯。
    Cell 的 Crossref 记录没有摘要，需要用 OpenAlex 按 DOI 回填。
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

    items = fetch_crossref_items(
        journal,
        require_abstract=True
    )

    papers = [
        paper for paper in (
            build_paper(item, journal)
            for item in items
        )
        if paper
    ]

    papers = [
        paper for paper in papers
        if len(paper["abstract"]) >= MIN_ABSTRACT_LENGTH
    ]

    # 带摘要的候选不足时，回退到完整记录并用 OpenAlex 回填
    if len(papers) < TARGET_PAPERS:

        fallback_items = fetch_crossref_items(
            journal,
            require_abstract=False
        )

        fallback_papers = [
            paper for paper in (
                build_paper(item, journal)
                for item in fallback_items
            )
            if paper
        ]

        papers = deduplicate_papers(
            papers + fallback_papers
        )

        fetch_openalex_abstracts(papers)

        papers = [
            paper for paper in papers
            if len(paper["abstract"]) >= MIN_ABSTRACT_LENGTH
        ]

    papers = filter_research_papers(papers)

    papers.sort(
        key=lambda paper: paper["date"],
        reverse=True
    )
    papers = papers[:TARGET_PAPERS]

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


def clean_ai_text(value):
    """清理模型输出中的行首缩进和多余空行。"""
    lines = [line.strip() for line in value.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


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
你是一名严谨的科研编辑，覆盖生物医学与人工智能方向。请只依据用户提供的论文元数据完成中文翻译与总结。
返回合法 JSON 对象，必须包含以下字符串字段：
title_zh：论文标题的简体中文翻译；
abstract_zh：摘要的完整简体中文翻译；如果没有摘要，明确写“该论文暂未提供公开摘要”；
summary_zh：直接面向读者写 3 至 5 段中文导读，依次说明研究问题、研究设计或方法、主要发现、意义与局限。
导读中不要复述任务、不要提到“用户提供的摘要”，也不要写大段“信息有限”之类的自我说明；确实缺少某方面信息时，用一句话自然带过。
不得编造用户没有提供的实验数据、结论或研究方法。
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
    return {key: clean_ai_text(parsed[key]) for key in required}


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
