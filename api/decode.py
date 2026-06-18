# -*- coding: utf-8 -*-
"""
超星学习通数据解析模块（精简版）

负责解析超星平台的课程、章节、任务点、题目等数据，并转换为结构化数据。
相比 chaoxing-fanya 原版，本版本移除了视频/直播任务的复杂处理与本地 OCR 依赖，
仅保留 PDF 抽题所需的最小解析能力，并完整保留题目解析 + 字体反加密逻辑。
"""
import json
import re
from typing import List, Dict, Tuple, Any, Optional, Union

from bs4 import BeautifulSoup, NavigableString

from api.font_decoder import FontDecoder
from api.logger import logger


# ==================== 通用工具 ====================

def _normalize_bool(value: Union[str, bool, int, float]) -> bool:
    """统一转换布尔值，避免字符串 'false' 被当成 True"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y", "passed"}
    return False


# ==================== 课程列表 ====================

def decode_course_list(html_text: str) -> List[Dict[str, str]]:
    """解析课程列表页面，提取课程信息"""
    logger.trace("开始解码课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("div.course")
    course_list = []

    for course in raw_courses:
        # 跳过未开放课程
        if course.select_one("a.not-open-tip") or course.select_one("div.not-open-tip"):
            continue
        try:
            course_detail = {
                "id": course.attrs.get("id", ""),
                "info": course.attrs.get("info", ""),
                "roleid": course.attrs.get("roleid", ""),
                "clazzId": course.select_one("input.clazzId").attrs["value"],
                "courseId": course.select_one("input.courseId").attrs["value"],
                "cpi": re.findall(r"cpi=(.*?)&", course.select_one("a").attrs["href"])[0],
                "title": course.select_one("span.course-name").attrs["title"],
                "desc": course.select_one("p.margint10").attrs["title"] if course.select_one("p.margint10") else "",
                "teacher": course.select_one("p.color3").attrs["title"] if course.select_one("p.color3") else "",
            }
        except (AttributeError, KeyError, IndexError) as e:
            logger.debug(f"跳过无法解析的课程条目: {e}")
            continue
        course_list.append(course_detail)

    return course_list


def decode_course_folder(html_text: str) -> List[Dict[str, str]]:
    """解析二级课程列表页面，提取文件夹信息"""
    logger.trace("开始解码二级课程列表...")
    soup = BeautifulSoup(html_text, "lxml")
    raw_courses = soup.select("ul.file-list>li")
    course_folder_list = []

    for course in raw_courses:
        if not course.attrs.get("fileid"):
            continue
        rename_input = course.select_one("input.rename-input")
        course_folder_list.append({
            "id": course.attrs["fileid"],
            "rename": rename_input.attrs["value"] if rename_input else "",
        })

    return course_folder_list


# ==================== 章节列表 ====================

def decode_course_point(html_text: str) -> Dict[str, Any]:
    """解析章节列表页面，提取章节点信息"""
    logger.trace("开始解码章节列表...")
    soup = BeautifulSoup(html_text, "lxml")
    course_point: Dict[str, Any] = {"hasLocked": False, "points": []}

    for chapter_unit in soup.find_all("div", class_="chapter_unit"):
        points = _extract_points_from_chapter(chapter_unit)
        for point in points:
            if point.get("need_unlock", False):
                course_point["hasLocked"] = True
        course_point["points"].extend(points)

    return course_point


def _extract_points_from_chapter(chapter_unit) -> List[Dict[str, Any]]:
    """从章节单元中提取章节点信息"""
    point_list = []
    raw_points = chapter_unit.find_all("li")

    for raw_point in raw_points:
        point = raw_point.div
        if not point or "id" not in point.attrs:
            continue

        m = re.findall(r"^cur(\d{1,20})$", point.attrs["id"])
        if not m:
            continue
        point_id = m[0]
        title_el = point.select_one("a.clicktitle")
        point_title = title_el.text.replace("\n", "").strip() if title_el else ""

        job_count = 1
        need_unlock = False
        if point.select_one("input.knowledgeJobCount"):
            job_count = point.select_one("input.knowledgeJobCount").attrs["value"]
        elif point.select_one("span.bntHoverTips") and "解锁" in point.select_one("span.bntHoverTips").text:
            need_unlock = True

        is_finished = False
        if point.select_one("span.bntHoverTips") and "已完成" in point.select_one("span.bntHoverTips").text:
            is_finished = True

        point_list.append({
            "id": point_id,
            "title": point_title,
            "jobCount": job_count,
            "has_finished": is_finished,
            "need_unlock": need_unlock,
        })

    return point_list


# ==================== 任务点卡片 ====================

def decode_course_card(html_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """解析任务点列表页面，提取任务点信息（仅保留 workid 测验任务）"""
    logger.trace("开始解码任务点列表...")

    if "章节未开放" in html_text:
        return [], {"notOpen": True}

    temp = re.findall(r"mArg=\{(.*?)\};", html_text.replace(" ", ""))
    if not temp:
        return [], {}

    try:
        cards_data = json.loads("{" + temp[0] + "}")
    except json.JSONDecodeError:
        return [], {}

    if not cards_data:
        return [], {}

    job_info = _extract_job_info(cards_data)
    cards = cards_data.get("attachments", [])
    job_list = _process_attachment_cards(cards)

    return job_list, job_info


def _extract_job_info(cards_data: Dict[str, Any]) -> Dict[str, Any]:
    """从卡片数据中提取任务基本信息"""
    defaults = cards_data.get("defaults", {})
    if not defaults:
        return {}
    return {
        "ktoken": defaults.get("ktoken", ""),
        "mtEnc": defaults.get("mtEnc", ""),
        "defenc": defaults.get("defenc", ""),
        "cardid": defaults.get("cardid", ""),
        "cpi": defaults.get("cpi", ""),
        "qnenc": defaults.get("qnenc", ""),
        "knowledgeid": defaults.get("knowledgeid", ""),
    }


def _process_attachment_cards(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """处理附件任务卡片，抽题只关心 workid（测验/作业）任务"""
    job_list = []
    for card in cards:
        if card.get("job") is None:
            continue
        if "otherInfo" in card:
            card["otherInfo"] = card["otherInfo"].split("&")[0]
        card_type = card.get("type", "").lower()
        if card_type == "workid":
            job_list.append(_process_work_task(card))
    return job_list


def _process_work_task(card: Dict[str, Any]) -> Dict[str, Any]:
    """处理作业/测验类型任务"""
    return {
        "type": "workid",
        "jobid": card.get("jobid", ""),
        "otherinfo": card.get("otherInfo", ""),
        "mid": card.get("mid", ""),
        "enc": card.get("enc", ""),
        "aid": card.get("aid", ""),
        "title": card.get("property", {}).get("title", "章节测验"),
    }


# ==================== 题目解析（核心） ====================

def decode_questions_info(html_content: str) -> Dict[str, Any]:
    """
    解析题目信息，提取表单数据和问题列表（含正确答案，若页面提供）。

    返回字典中包含：
      - questions: 题目列表，每题含 id/title/options/type/answer/analysis
      - 以及若干表单隐藏字段（供提交时使用，本项目仅用于抽题展示）
    """
    soup = BeautifulSoup(html_content, "lxml")
    form_data = _extract_form_data(soup)

    has_font_encryption = bool(soup.find("style", id="cxSecretStyle"))
    font_decoder = None
    if has_font_encryption:
        try:
            font_decoder = FontDecoder(html_content)
        except Exception as e:
            logger.warning(f"字体解码器初始化失败: {e}")
            font_decoder = None

    questions = []
    form = soup.find("form")
    container = form if form else soup
    for div_tag in container.find_all("div", class_="singleQuesId"):
        question = _process_question(div_tag, font_decoder)
        if question and question.get("title"):
            questions.append(question)

    # 作业页结构（singleQuesId）未命中时，回退到考试/自测答题页结构解析
    if not questions:
        # 先尝试真实考试答题页（whiteDiv questionLi singleQuesId + stem_answer/singleoption）
        exam_page = decode_exam_page(soup, font_decoder)
        if exam_page.get("questions"):
            questions = exam_page["questions"]
        else:
            questions = decode_exam_questions(soup, font_decoder)

    form_data["questions"] = questions
    form_data["answerwqbid"] = ",".join([q["id"] for q in questions]) + ","
    return form_data


# ==================== 真实考试答题页解析（逐题加载页） ====================
#
# 超星自测/考试答题页（reVersionTestStartNew）一次只渲染“当前一题”，结构为：
#   <div class="whiteDiv questionLi singleQuesId" data="题目ID">
#     <h3 class="mark_name">序号. <span class="colorShallow">(单选题)</span> 题干...<img ...></h3>
#     <input id="typeNameXXX" value="单选题">
#     <div class="stem_answer">
#       <div class="... singleoption"><span data="A">A</span><div class="answer_p">选项内容<img ...></div></div>
#       ...
#     </div>
#   </div>
# 其余题目通过答题卡 li[onclick="getTheQuestionByStart(N,...)"] 切换（&start=N 整页跳转）。
# 题干/选项中的数学公式多为 <img src="https://p.ananas.chaoxing.com/...png">，需保留 URL。

_EXAM_TYPE_NAME_MAP = {
    "单选题": "single", "多选题": "multiple", "判断题": "judgement",
    "填空题": "completion", "简答题": "shortanswer", "论述题": "shortanswer",
    "名词解释": "shortanswer", "问答题": "shortanswer", "计算题": "shortanswer",
    "分析题": "shortanswer", "其它": "unknown", "其他": "unknown",
}


def _extract_rich_text(element, font_decoder=None) -> str:
    """提取元素文本，<img> 转为 【图片: URL】 标记保留（供 PDF 下载内嵌），支持字体解码。"""
    if not element:
        return ""
    parts = []
    for item in element.descendants:
        if isinstance(item, NavigableString):
            parts.append(item.string or "")
        elif getattr(item, "name", None) == "img":
            url = item.get("src") or item.get("data-original") or ""
            if url:
                if url.startswith("//"):
                    url = "https:" + url
                parts.append(f"【图片: {url}】")
    text = "".join(parts)
    text = re.sub(r"[\r\t\n]+", "", text).strip()
    if font_decoder:
        try:
            text = font_decoder.decode(text)
        except Exception:
            pass
    return text


def exam_page_total(soup_or_html) -> int:
    """读取答题卡里的题目总数（li[onclick^=getTheQuestionByStart] 的数量）。"""
    soup = BeautifulSoup(soup_or_html, "lxml") if isinstance(soup_or_html, str) else soup_or_html
    lis = soup.find_all("li", onclick=re.compile(r"getTheQuestionByStart"))
    return len(lis)


def decode_exam_page(soup_or_html, font_decoder=None) -> Dict[str, Any]:
    """
    解析“逐题加载”的真实考试答题页，返回：
      {"questions": [一题], "total": 总题数, "paper_id": str, "test_paper_id": str}
    若结构不匹配返回空 questions。
    """
    soup = BeautifulSoup(soup_or_html, "lxml") if isinstance(soup_or_html, str) else soup_or_html

    # 题目容器：whiteDiv + questionLi + singleQuesId（同时具备 stem_answer 才是真实答题页）
    container = None
    for div in soup.find_all("div", class_="questionLi"):
        if div.find("div", class_="stem_answer") or div.find("h3", class_="mark_name"):
            container = div
            break
    result: Dict[str, Any] = {
        "questions": [], "total": exam_page_total(soup),
        "paper_id": "", "test_paper_id": "",
    }
    pid = soup.find("input", id="paperId")
    tpid = soup.find("input", id="testPaperId")
    if pid and pid.get("value"):
        result["paper_id"] = pid["value"]
    if tpid and tpid.get("value"):
        result["test_paper_id"] = tpid["value"]

    if not container:
        return result

    q = _process_exam_single(container, soup, font_decoder)
    if q and q.get("title"):
        result["questions"] = [q]
    return result


def _process_exam_single(container, soup, font_decoder=None) -> Optional[Dict[str, Any]]:
    """解析真实考试答题页的单道题。"""
    qid = container.attrs.get("data", "") or ""

    # 题型：优先 typeNameXXX 隐藏域，其次题干里的 “(单选题)”
    q_type = "unknown"
    type_input = None
    if qid:
        type_input = soup.find("input", id=f"typeName{qid}")
    if type_input is None:
        type_input = soup.find("input", id=re.compile(r"^typeName\d+$"))
    if type_input and type_input.get("value"):
        q_type = _EXAM_TYPE_NAME_MAP.get(type_input["value"].strip(), "unknown")

    # 题干：h3.mark_name，去掉序号与 (单选题) 前缀，保留图片
    title = ""
    mark = container.find("h3", class_="mark_name")
    if mark:
        # 单独剥离题型标注 span
        span = mark.find("span", class_="colorShallow")
        if span:
            if q_type == "unknown":
                q_type = _infer_type_from_text(span.get_text())
            span.extract()
        title = _extract_rich_text(mark, font_decoder)
        title = _strip_type_prefix(title)
    if q_type == "unknown":
        q_type = _infer_type_from_text(title)

    # 选项：div.stem_answer 下的 singleoption / 选项块
    options: List[str] = []
    stem = container.find("div", class_="stem_answer")
    if stem:
        opt_divs = stem.find_all("div", class_=re.compile(r"singleoption|multioption|answerBg"))
        seen_letters = set()
        for od in opt_divs:
            letter_el = od.find("span", attrs={"data": True})
            letter = (letter_el.get("data") or "").strip() if letter_el else ""
            # 选项正文
            content_el = od.find("div", class_=re.compile(r"answer_p"))
            content = _extract_rich_text(content_el, font_decoder) if content_el else ""
            if not content:
                # 兜底：取整块去掉字母
                content = _extract_rich_text(od, font_decoder)
                if letter and content.startswith(letter):
                    content = content[len(letter):].lstrip(".、) ")
            if letter and letter in seen_letters:
                continue
            if letter:
                seen_letters.add(letter)
                options.append(f"{letter}. {content}")
            elif content:
                options.append(content)

    # 答案：做题页通常为空（answerXXX 隐藏域为空），尽力尝试
    answer = ""
    if qid:
        ans_input = soup.find("input", id=f"answer{qid}")
        if ans_input and (ans_input.get("value") or "").strip():
            answer = ans_input["value"].strip()

    return {
        "id": str(qid),
        "title": title,
        "options": options,
        "type": q_type,
        "type_code": "",
        "answer": answer,
        "analysis": "",
    }



# ==================== 考试 / 自测答题页解析（结构不同于作业页） ====================

# 考试答题页里题目容器可能用到的 class（按出现概率排序）
_EXAM_Q_SELECTORS = [
    "div.questionLi",
    "div.TiMu",
    "div.timu",
    "div.Cy_ulTk",
    "div.queText",
]
# 题干元素候选 class
_EXAM_TITLE_SELECTORS = [
    "div.mark_name", "h3.mark_name", "div.Zy_TItle", "div.Cy_TItle",
    "div.qtContent", "h3", "div.queTitle",
]
# 选项容器候选 class
_EXAM_OPTION_SELECTORS = ["ul.mark_letter", "ul.Cy_ulb", "ul.cyTk", "ul"]


def decode_exam_questions(soup_or_html, font_decoder=None) -> List[Dict[str, Any]]:
    """
    解析超星考试 / 自测答题页（reVersionTestStartNew / lookPaper）题目。

    该页面与作业页（singleQuesId）结构不同，常见为：
        <div class="questionLi" data="题型码" ...>
            <div class="mark_name">序号 / 题型 / 题干</div>
            <ul class="mark_letter"><li>A. xxx</li>...</ul>
        </div>
    本函数对多种容器/题干/选项 class 做兼容，尽量稳健。
    """
    if isinstance(soup_or_html, str):
        soup = BeautifulSoup(soup_or_html, "lxml")
    else:
        soup = soup_or_html

    containers = []
    for sel in _EXAM_Q_SELECTORS:
        name, cls = sel.split(".", 1)
        found = soup.find_all(name, class_=cls)
        if found:
            containers = found
            logger.debug(f"考试题目容器命中: {sel}（{len(found)} 个）")
            break

    questions: List[Dict[str, Any]] = []
    for idx, node in enumerate(containers):
        q = _process_exam_question(node, idx, font_decoder)
        if q and q.get("title"):
            questions.append(q)
    if questions:
        logger.info(f"考试页结构解析到 {len(questions)} 题")
    return questions


def _process_exam_question(node, idx: int, font_decoder=None) -> Optional[Dict[str, Any]]:
    """解析单个考试题目容器。"""
    # 容器的 data 属性在考试页通常是“题目ID”（长数字），不是题型码，
    # 故题型优先从题干文字推断（如 “（单选题）”），仅当 data 是已知的小题型码时才采用。
    raw_data = node.attrs.get("data", "") or ""
    type_attr = node.attrs.get("type", "")

    # 题干
    title_el = None
    for sel in _EXAM_TITLE_SELECTORS:
        name, cls = (sel.split(".", 1) + [""])[:2]
        title_el = node.find(name, class_=cls) if cls else node.find(name)
        if title_el:
            break
    raw_title = _extract_title(title_el, font_decoder) if title_el else ""

    # 题型推断：优先文字推断；其次题干元素/容器上明确的题型码（短码，非长数字ID）
    inferred = _infer_type_from_text(raw_title)
    q_type = inferred
    if q_type == "unknown":
        candidate = ""
        if title_el and title_el.attrs.get("data", ""):
            candidate = title_el.attrs.get("data", "")
        elif type_attr:
            candidate = type_attr
        elif raw_data and not raw_data.isdigit():
            candidate = raw_data
        elif raw_data and raw_data.isdigit() and len(raw_data) <= 2:
            candidate = raw_data  # 短数字才可能是题型码
        if candidate not in ("", None):
            mapped = _get_question_type(candidate)
            if mapped != "unknown":
                q_type = mapped
    # 去掉题干里的题型前缀
    title = _strip_type_prefix(raw_title)

    # 选项
    options: List[str] = []
    opt_ul = None
    for sel in _EXAM_OPTION_SELECTORS:
        name, cls = (sel.split(".", 1) + [""])[:2]
        opt_ul = node.find(name, class_=cls) if cls else node.find(name)
        if opt_ul and opt_ul.find_all("li"):
            break
    if opt_ul:
        for li in opt_ul.find_all("li"):
            txt = _extract_choices(li, font_decoder)
            if txt:
                options.append(txt)
    options = [o for o in options if o]

    # 答题页通常不显示答案（提交前），尽力尝试
    answer, analysis = _extract_answer_and_analysis(node, font_decoder)

    qid = raw_data or node.attrs.get("id", "") or f"q{idx+1}"
    return {
        "id": str(qid),
        "title": title,
        "options": options,
        "type": q_type,
        "type_code": str(raw_data),
        "answer": answer,
        "analysis": analysis,
    }


def _infer_type_from_text(text: str) -> str:
    """从题干文字里的题型标注推断题型。"""
    if not text:
        return "unknown"
    head = text[:20]
    if "单选" in head:
        return "single"
    if "多选" in head:
        return "multiple"
    if "判断" in head:
        return "judgement"
    if "填空" in head:
        return "completion"
    if any(k in head for k in ("简答", "论述", "名词解释", "问答")):
        return "shortanswer"
    return "unknown"


def _strip_type_prefix(title: str) -> str:
    """去掉题干开头的 “（单选题）/【多选题】/1.” 等前缀（可能多种前缀叠加，循环剥离）。"""
    if not title:
        return ""
    t = title.strip()
    type_re = re.compile(r"^\s*[（(【\[]\s*(单选|多选|判断|填空|简答|论述|问答|名词解释)题?\s*[）)】\]]\s*")
    num_re = re.compile(r"^\s*\d{1,3}\s*[、.．)]\s*")
    for _ in range(4):  # 最多剥 4 层，足够覆盖 “1.（单选题）” 这类组合
        new = num_re.sub("", t)
        new = type_re.sub("", new)
        if new == t:
            break
        t = new
    return t.strip()


def _extract_form_data(soup: BeautifulSoup) -> Dict[str, Any]:
    """从 BeautifulSoup 对象中提取表单隐藏字段"""
    form_data: Dict[str, Any] = {}
    form_tag = soup.find("form")
    if not form_tag:
        return form_data
    for input_tag in form_tag.find_all("input"):
        if "name" not in input_tag.attrs or "answer" in input_tag.attrs["name"]:
            continue
        form_data[input_tag.attrs["name"]] = input_tag.attrs.get("value", "")
    return form_data


def _process_question(div_tag, font_decoder=None) -> Optional[Dict[str, Any]]:
    """处理单个问题，解析题干、选项、题型与正确答案。"""
    question_id = div_tag.attrs.get("data", "")
    timu = div_tag.find("div", class_="TiMu")
    q_type_code = timu.attrs.get("data", "") if timu else ""
    q_type = _get_question_type(q_type_code)

    title_div = div_tag.find("div", class_="Zy_TItle")
    options_list = div_tag.find("ul").find_all("li") if div_tag.find("ul") else []

    q_title = _extract_title(title_div, font_decoder)
    q_options = [_extract_choices(li, font_decoder) for li in options_list]
    q_options = [o for o in q_options if o]
    q_options.sort()

    # 尝试解析正确答案与解析（自测/作业回顾页通常提供）
    answer, analysis = _extract_answer_and_analysis(div_tag, font_decoder)

    return {
        "id": question_id,
        "title": q_title,
        "options": q_options,            # list[str]，每项形如 "A. xxx"
        "type": q_type,
        "type_code": q_type_code,
        "answer": answer,                # str，正确答案（可能为空）
        "analysis": analysis,            # str，答案解析（可能为空）
    }


def _get_question_type(type_code: str) -> str:
    """根据题型代码返回题型名称"""
    type_map = {
        "0": "single",       # 单选题
        "1": "multiple",     # 多选题
        "2": "completion",   # 填空题
        "3": "judgement",    # 判断题
        "4": "shortanswer",  # 简答题
    }
    if type_code in type_map:
        return type_map[type_code]
    logger.debug(f"未知题型代码 -> {type_code}")
    return "unknown"


def _clean_text(raw: str) -> str:
    return raw.replace("\r", "").replace("\t", "").replace("\xa0", " ").strip()


def _extract_title(element, font_decoder=None) -> str:
    """提取题干内容，支持图片占位与加密字体解码"""
    if not element:
        return ""
    content = []
    for item in element.descendants:
        if isinstance(item, NavigableString):
            content.append(item.string or "")
        elif getattr(item, "name", None) == "img":
            img_url = item.get("src", "")
            if img_url:
                content.append(f'【图片: {img_url}】')
    raw_content = "".join(content)
    cleaned = raw_content.replace("\r", "").replace("\t", "").replace("\n", "").strip()
    # 去掉题型前缀，如 "（单选题）"
    if font_decoder:
        try:
            return font_decoder.decode(cleaned)
        except Exception:
            return cleaned
    return cleaned


def _extract_choices(element, font_decoder=None) -> str:
    """提取选项内容，支持加密字体解码"""
    if not element:
        return ""
    choice = element.get("aria-label") or element.get_text()
    if not choice:
        return ""
    cleaned = re.sub(r"[\r\t\n]", "", choice)
    if font_decoder:
        try:
            cleaned = font_decoder.decode(cleaned)
        except Exception:
            pass
    cleaned = cleaned.strip()
    if cleaned.endswith("选择"):
        cleaned = cleaned[:-2].rstrip()
    return cleaned


def _extract_answer_and_analysis(div_tag, font_decoder=None) -> Tuple[str, str]:
    """
    从题目 div 中尽力解析正确答案与解析。

    超星不同页面结构差异较大，这里覆盖常见几种：
      - 含 class 包含 'Cy_ulTk' / 'cyTk' 的答案块
      - 文本中出现 "正确答案：X" / "我的答案" 等
    解析不到时返回空字符串，由上层决定如何展示。
    """
    answer = ""
    analysis = ""

    # 1) 常见答案容器
    for cls in ["Cy_ulTk", "Py_answer", "cyTk", "mark_answer", "Cy_ul"]:
        block = div_tag.find("div", class_=cls)
        if block:
            text = _clean_text(block.get_text(separator=" "))
            if text:
                m = re.search(r"正确答案[：:]\s*([^\s。]+)", text)
                if m:
                    answer = m.group(1).strip()
                ma = re.search(r"(答案解析|解析)[：:]\s*(.+)", text)
                if ma:
                    analysis = ma.group(2).strip()
                if not answer and not analysis:
                    analysis = text
                break

    # 2) 兜底：整块文本正则
    if not answer:
        whole = div_tag.get_text(separator=" ")
        m = re.search(r"正确答案[：:]\s*([A-Za-z对错正确错误√×]+)", whole)
        if m:
            answer = m.group(1).strip()

    if font_decoder and answer:
        try:
            answer = font_decoder.decode(answer)
        except Exception:
            pass
    if font_decoder and analysis:
        try:
            analysis = font_decoder.decode(analysis)
        except Exception:
            pass

    return answer, analysis
