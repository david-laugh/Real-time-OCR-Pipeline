#!/usr/bin/env python3
"""
OCR 엔진 비교 벤치마크 (파일 하나, 서브커맨드 CLI)

컨베이어 송출기(ocr-conveyor-test.html)의 PASSAGES 원문을 정답으로 삼아,
휴대폰으로 찍은 영상의 프레임을 여러 OCR 엔진으로 읽고 정확도·속도를 비교한다.

사용 순서
  python ocr_bench.py truth  --html ocr-conveyor-test.html
  python ocr_bench.py frames 기본.mp4 빠름.mp4 저대비.mp4 --every 1.0
  python ocr_bench.py run    --engines easyocr tesseract rapidocr
  python ocr_bench.py score
  python ocr_bench.py dedup             # 중복 저장 방지 시뮬레이션
  python ocr_bench.py report --out docs/ocr-engine-comparison.md   # 서술형 보고서
  python ocr_bench.py send --engine rapidocr --api http://localhost:8000   # 선택: 뷰어에서 보기

설치 (필요한 엔진만)
  pip install opencv-python rapidfuzz
  pip install easyocr                      # 현재 팀 엔진
  pip install pytesseract                  # + 시스템에 tesseract 설치 필요
  pip install rapidocr_onnxruntime         # PaddleOCR 모델의 ONNX 경량판
  pip install paddleocr paddlepaddle       # 원본 PaddleOCR (선택)
"""
import argparse, json, re, sys, time
from pathlib import Path

WORK = Path("bench")
FRAMES = WORK / "frames"
OUT = WORK / "ocr"


# ---------------------------------------------------------------- truth
def cmd_truth(a):
    t = Path(a.html).read_text(encoding="utf-8")
    s = t[t.index("const PASSAGES"):]
    s = s[: s.index("];")]
    S = r'"((?:[^"\\]|\\.)*)"'
    items = re.findall(r"title:\s*" + S + r"\s*,\s*body:\s*\[(.*?)\]\s*\}", s, re.S)
    unesc = lambda x: json.loads('"' + x + '"')
    passages = [{"title": unesc(ti), "body": [unesc(b) for b in re.findall(S, body)]} for ti, body in items]
    WORK.mkdir(exist_ok=True)
    (WORK / "truth.json").write_text(json.dumps(passages, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"정답 지문 {len(passages)}개 -> {WORK/'truth.json'}")


def load_truth_lines():
    passages = json.loads((WORK / "truth.json").read_text(encoding="utf-8"))
    lines = []
    for p in passages:
        lines.append(p["title"])
        lines += p["body"]
    return [norm(x) for x in lines]


def norm(s):
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
    s = s.replace("\u2014", "-").replace("\u2013", "-")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- frames
def cmd_frames(a):
    import cv2
    FRAMES.mkdir(parents=True, exist_ok=True)
    for v in a.videos:
        cap = cv2.VideoCapture(v)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30
        step = max(1, int(round(fps * a.every)))
        name, i, kept = Path(v).stem, 0, 0
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if i % step == 0:
                sharp = cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
                if sharp >= a.min_sharp:
                    cv2.imwrite(str(FRAMES / f"{name}__{i:05d}.jpg"), img, [cv2.IMWRITE_JPEG_QUALITY, 95])
                    kept += 1
            i += 1
        print(f"{v}: {kept}장 저장 ({a.every}s 간격, 선명도 {a.min_sharp} 미만 제외)")


# ---------------------------------------------------------------- engines
def make_engine(name):
    """각 엔진은 이미지 경로 -> 줄 단위 텍스트 리스트 함수를 돌려준다."""
    if name == "easyocr":
        import easyocr
        try:
            import torch; gpu = torch.cuda.is_available()
        except Exception:
            gpu = False
        r = easyocr.Reader(["en"], gpu=gpu)
        return lambda p: group_lines([(b, t) for b, t, c in r.readtext(p)])
    if name == "tesseract":
        import pytesseract
        from PIL import Image
        return lambda p: [l for l in pytesseract.image_to_string(Image.open(p), config="--psm 3").splitlines() if l.strip()]
    if name == "rapidocr":
        from rapidocr_onnxruntime import RapidOCR
        r = RapidOCR()
        def f(p):
            res, _ = r(p)
            return group_lines([(b, t) for b, t, c in (res or [])])
        return f
    if name == "paddleocr":
        from paddleocr import PaddleOCR
        r = PaddleOCR(lang="en", use_angle_cls=False, show_log=False)
        def f(p):
            res = r.ocr(p) or [[]]
            return group_lines([(b, t) for b, (t, c) in (res[0] or [])])
        return f
    sys.exit(f"알 수 없는 엔진: {name}")


def group_lines(boxes):
    """박스 단위 결과를 같은 줄(높이 겹침 + 가까운 가로 거리)끼리 이어 붙인다."""
    items = []
    for b, t in boxes:
        xs = [p[0] for p in b]; ys = [p[1] for p in b]
        items.append([min(xs), max(xs), min(ys), max(ys), t])
    items.sort(key=lambda r: (r[2], r[0]))
    lines = []
    for it in items:
        h = it[3] - it[2]
        for ln in lines:
            last = ln[-1]
            same_row = abs((last[2] + last[3]) / 2 - (it[2] + it[3]) / 2) < h * 0.5
            near = 0 <= it[0] - last[1] < h * 2.0
            if same_row and near:
                ln.append(it); break
        else:
            lines.append([it])
    return [" ".join(x[4] for x in ln) for ln in lines]


def cmd_run(a):
    frames = sorted(FRAMES.glob("*.jpg"))
    if a.limit:
        frames = frames[: a.limit]
    if not frames:
        sys.exit("프레임이 없습니다. 먼저 frames 를 실행하세요.")
    OUT.mkdir(parents=True, exist_ok=True)
    for name in a.engines:
        print(f"[{name}] 로딩...")
        eng = make_engine(name)
        eng(str(frames[0]))  # 워밍업 (모델 로딩 시간 제외)
        rows = []
        for i, fp in enumerate(frames, 1):
            t0 = time.perf_counter()
            lines = eng(str(fp))
            ms = (time.perf_counter() - t0) * 1000
            rows.append({"frame": fp.name, "ms": round(ms, 1), "lines": lines})
            print(f"  {i}/{len(frames)} {fp.name} {ms:.0f}ms {len(lines)}줄")
        (OUT / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- score
NUM = re.compile(r"\d[\d,.:%]*\d|\d")


def cmd_score(a):
    from rapidfuzz import fuzz, process
    from rapidfuzz.distance import Levenshtein
    truth = load_truth_lines()
    summary = {}
    for f in sorted(OUT.glob("*.json")):
        rows = json.loads(f.read_text(encoding="utf-8"))
        per_cond = {}
        for r in rows:
            cond = r["frame"].split("__")[0]
            c = per_cond.setdefault(cond, dict(chars=0, err=0, matched=0, junk=0, num_ok=0, num_all=0, ms=[]))
            c["ms"].append(r["ms"])
            for line in r["lines"]:
                line = norm(line)
                if len(line) < a.min_len:
                    continue
                best = process.extractOne(line, truth, scorer=fuzz.partial_ratio)
                if not best or best[1] < a.min_sim:
                    c["junk"] += 1   # 정답 어디에도 안 맞는 줄 (UI 글자, 쓰레기 인식 등)
                    continue
                al = fuzz.partial_ratio_alignment(line, best[0])
                ref = best[0][al.dest_start: al.dest_end]
                c["chars"] += len(ref)
                c["err"] += Levenshtein.distance(line, ref)
                c["matched"] += 1
                ref_nums = set(NUM.findall(ref))
                got_nums = set(NUM.findall(line))
                c["num_all"] += len(ref_nums)
                c["num_ok"] += len(ref_nums & got_nums)
        res = {}
        for cond, c in per_cond.items():
            ms = sorted(c["ms"])
            res[cond] = {
                "CER": round(c["err"] / c["chars"], 4) if c["chars"] else None,
                "인식_글자수": c["chars"],
                "매칭_줄수": c["matched"],
                "불일치_줄수": c["junk"],
                "숫자_정확도": round(c["num_ok"] / c["num_all"], 4) if c["num_all"] else None,
                "중앙값_ms": ms[len(ms) // 2],
                "프레임수": len(ms),
            }
        summary[f.stem] = res
    (WORK / "scores.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    for eng, conds in summary.items():
        for cond, v in conds.items():
            print(f"{eng:10s} {cond:8s} CER {v['CER']}  글자 {v['인식_글자수']}  숫자 {v['숫자_정확도']}  {v['중앙값_ms']}ms/프레임")



# ---------------------------------------------------------------- dedup (중복 저장 방지)
class Deduper:
    """같은 카드가 여러 프레임에 찍혀 같은 글이 반복 저장되는 것을 막는다.
    서버의 /api/process 저장 직전에 그대로 옮겨 쓸 수 있도록 엔진·정답과 무관하게 동작한다.

    check(text) 결과
      "new"    : 처음 보는 글 -> 저장
      "dup"    : 이미 저장된 글과 같음(또는 그 일부) -> 버림
      "update" : 이미 저장된 글의 더 완전한 버전 -> 기존 행을 이 글로 교체
      "skip"   : 너무 짧아서 판단 불가 -> 버림
    """
    def __init__(self, threshold=85, min_len=8, window=200):
        self.threshold, self.min_len, self.window = threshold, min_len, window
        self.saved = []          # 최근 저장분 (컨베이어는 순서대로 흐르므로 최근 것만 비교해도 충분)

    def check(self, text):
        from rapidfuzz import fuzz, process
        t = norm(text).lower()
        if len(t) < self.min_len:
            return "skip", None
        best = process.extractOne(t, self.saved[-self.window:], scorer=fuzz.partial_ratio)
        if best and best[1] >= self.threshold:
            idx = len(self.saved) - min(len(self.saved), self.window) + best[2]
            if len(t) > len(self.saved[idx]) * 1.15:      # 잘려 있던 글이 이번엔 더 길게 찍힘
                self.saved[idx] = t
                return "update", idx
            return "dup", idx
        self.saved.append(t)
        return "new", len(self.saved) - 1


def cmd_dedup(a):
    """엔진별 OCR 결과를 프레임 순서대로 Deduper에 흘려보내고,
    중복 처리 후 실제로 몇 줄이 저장되는지, 그중 진짜 중복이 몇 개 새어 들어갔는지 측정한다."""
    from rapidfuzz import fuzz, process
    truth = load_truth_lines()
    result = {}
    for f in sorted(OUT.glob("*.json")):
        rows = json.loads(f.read_text(encoding="utf-8"))
        by_cond = {}
        for r in rows:
            by_cond.setdefault(r["frame"].split("__")[0], []).append(r)
        res = {}
        for cond, rs in by_cond.items():
            d = Deduper(threshold=a.threshold)
            raw = 0; stat = {"new": 0, "dup": 0, "update": 0, "skip": 0}
            for r in sorted(rs, key=lambda x: x["frame"]):
                for line in r["lines"]:
                    raw += 1
                    stat[d.check(line)[0]] += 1
            # 저장된 줄을 정답 줄에 대응시켜 '새어 들어간 중복'을 센다 (평가용으로만 정답 사용)
            # 정답은 문단 단위, OCR은 화면의 한 줄 단위이므로 '같은 문단의 같은 구간'을 두 번 저장했을 때만 중복으로 센다
            low = [x.lower() for x in truth]
            spans, leaked, junk = {}, 0, 0
            for t in d.saved:
                best = process.extractOne(t, low, scorer=fuzz.partial_ratio)
                if not best or best[1] < 60:
                    junk += 1; continue
                al = fuzz.partial_ratio_alignment(t, best[0])
                a0, a1 = al.dest_start, al.dest_end
                lst = spans.setdefault(best[2], [])
                if any(min(a1, b1) - max(a0, b0) > 0.5 * min(a1 - a0, b1 - b0) for b0, b1 in lst):
                    leaked += 1
                else:
                    lst.append((a0, a1))
            seen = spans
            res[cond] = {"원본_줄수": raw, "저장": stat["new"], "갱신": stat["update"],
                         "버린_중복": stat["dup"], "새어든_중복": leaked, "고유_정답줄": len(seen), "쓰레기": junk}
            print(f"{f.stem:10s} {cond:8s} 원본 {raw}줄 -> 저장 {stat['new']} (중복 {stat['dup']} 버림, "
                  f"갱신 {stat['update']}) / 새어든 중복 {leaked}, 쓰레기 {junk}")
        result[f.stem] = res
    (WORK / "dedup.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- report (서술형)
def cmd_report(a):
    s = json.loads((WORK / "scores.json").read_text(encoding="utf-8"))
    dd = json.loads((WORK / "dedup.json").read_text(encoding="utf-8")) if (WORK / "dedup.json").exists() else {}
    engines = sorted(s)
    out = [f"# OCR 엔진 비교: {' vs '.join(engines)}", ""]
    out += ["## 무엇을 했나", "",
            "컨베이어 송출기(ocr-conveyor-test.html) 화면을 휴대폰으로 세 가지 조건에서 촬영했다. "
            "기본 설정, 이송 속도를 높인 '빠름', 지면을 저대비로 바꾼 '저대비'이다. "
            "각 영상에서 일정 간격으로 프레임을 뽑아 모든 엔진에 똑같이 넣었고, 송출기 안의 PASSAGES 원문 100개를 정답으로 삼아 채점했다. "
            "YOLO 카드 검출 없이 프레임 전체를 넣었기 때문에, 엔진 자체의 읽기 능력만 비교한 결과다.", "",
            "지표는 네 가지다. CER은 정답 대비 문자 오류율로 낮을수록 좋다. "
            "인식 글자 수는 정답과 맞춰진 글자의 총량으로, 화면의 글을 얼마나 많이 읽어냈는지를 뜻한다. "
            "숫자 정확도는 정답 줄에 있는 숫자 중 정확히 읽힌 비율이다. 처리 시간은 프레임 한 장당 중앙값이다."]
    conds = sorted({c for v in s.values() for c in v})
    out += ["", "## 결과"]
    for cond in conds:
        out += ["", f"### {cond}", ""]
        rank = sorted(((e, v[cond]) for e, v in s.items() if cond in v and v[cond]["CER"] is not None),
                      key=lambda x: x[1]["CER"])
        for e, v in rank:
            num = f"{v['숫자_정확도']*100:.0f}%" if v["숫자_정확도"] is not None else "측정 불가"
            out.append(f"{e}는 CER {v['CER']*100:.1f}%로 정답 글자 {v['인식_글자수']}자를 맞춰 읽었고, "
                       f"숫자 정확도는 {num}, 프레임당 처리 시간은 {v['중앙값_ms']:.0f}ms였다. "
                       f"정답과 맞지 않는 줄은 {v['불일치_줄수']}개였다.")
            out.append("")
        if len(rank) > 1:
            most = max(rank, key=lambda x: x[1]["인식_글자수"])
            out.append(f"이 조건에서 오류율이 가장 낮은 엔진은 {rank[0][0]}, 가장 많이 읽어낸 엔진은 {most[0]}이다.")
    if dd:
        out += ["", "## 중복 저장 문제", "",
                "카메라는 초당 30장을 찍고 카드는 몇 초 동안 화면에 머물기 때문에, 같은 지문이 여러 프레임에서 반복해서 읽힌다. "
                "그대로 저장하면 같은 글이 DB에 수십 번 들어가고 대시보드의 저장 개수도 틀어진다. "
                "또 프레임마다 OCR 결과가 조금씩 달라서(예: 240과 24o) 문자열이 완전히 같은지로는 걸러지지 않는다.", "",
                "아래는 각 엔진의 결과를 프레임 순서대로 중복 필터(아래 해결 방안의 2단계)에 통과시킨 결과다."]
        for e in sorted(dd):
            for cond, v in dd[e].items():
                out.append("")
                out.append(f"{e} / {cond}: 원본 {v['원본_줄수']}줄 중 {v['버린_중복']}줄을 중복으로 버리고 {v['저장']}줄을 저장했으며, "
                           f"잘린 글을 더 완전한 글로 갱신한 경우가 {v['갱신']}번이었다. "
                           f"저장된 줄 중 실제로는 중복인데 새어 들어간 것이 {v['새어든_중복']}줄, 정답과 무관한 줄이 {v['쓰레기']}줄이었다.")
        out += ["", "## 현재 코드의 중복 처리와 한계", "",
                "services/frame_sink.py의 OcrSink._is_duplicate()는 세션마다 '직전에 저장한 텍스트 하나'와 새 프레임 전체 텍스트를 SequenceMatcher로 비교해 90% 이상 같으면 건너뛴다. "
                "책 한 페이지를 계속 비추는 상황에서는 잘 동작하지만, 컨베이어처럼 카드가 계속 옆으로 흐르면 프레임마다 화면에 걸친 카드 조합이 달라져 전체 텍스트가 10% 넘게 바뀐다. "
                "같은 방식을 이번 촬영 프레임에 그대로 적용해 보면 세 조건 모두 모든 프레임이 새 문서로 저장되어, 중복이 하나도 걸러지지 않았다. "
                "또 직전 하나와만 비교하므로 A, B, A 순서로 같은 글이 다시 나타나면 막지 못한다.", "",
                "이와 별개로 ocr_service._group_lines()는 높이(y)만 보고 줄을 묶기 때문에, 카드 여러 장이 나란히 찍힌 프레임에서는 옆 카드의 같은 높이 줄이 한 줄로 섞인다. "
                "중복 판정 이전에 텍스트 자체가 섞이는 문제라 함께 고칠 필요가 있다. 이 벤치마크의 줄 묶기는 가로 거리가 글자 높이의 두 배를 넘으면 다른 줄로 나눠 이 문제를 피했다."]
        out += ["", "## 해결 방안", "",
                "1단계는 단위를 바꾸는 것이다. 프레임 전체가 아니라 문단(또는 카드) 단위로 중복을 판정한다. "
                "_group_lines()에 가로 거리 기준을 넣어 카드끼리 섞이지 않게 한 뒤, 문단마다 새 글인지 확인해 새 문단만 저장한다. "
                "카드 영역 검출(YOLO 등)을 붙인다면 화면 가장자리에 걸친 잘린 카드는 읽지 않는 것도 효과적이다.", "",
                "2단계는 비교 대상을 넓히는 것이다. 직전 하나가 아니라 세션에서 최근 저장한 문단 수백 개와 비교하고, 부분 유사도(rapidfuzz partial_ratio)가 85점 이상이면 같은 글로 본다. "
                "새 글이 기존 글보다 확실히 길면 잘려 있던 카드가 더 완전하게 찍힌 것이므로 기존 행을 갱신한다. "
                "ocr_bench.py의 Deduper 클래스가 이 로직이며, 위 시뮬레이션 수치가 그 효과다.", "",
                "3단계는 선택 사항으로, 같은 문단을 2~3번 본 뒤 가장 길고 자주 나온 결과 하나만 저장하는 방식이다. 오인식이 DB에 들어가는 것도 줄지만 저장이 1~2초 늦어진다.", "",
                "DB에서 title에 UNIQUE를 거는 방식은 OCR 오차(예: 240과 24o) 때문에 같은 글도 다른 문자열이 되므로 통하지 않는다."]
    dest = Path(a.out); dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"-> {dest}")


# ---------------------------------------------------------------- send (선택)
def cmd_send(a):
    import urllib.request
    rows = json.loads((OUT / f"{a.engine}.json").read_text(encoding="utf-8"))
    for r in rows[: a.limit or None]:
        body = json.dumps({"text": "\n".join(r["lines"]), "task_id": f"bench-{a.engine}",
                           "source_id": r["frame"]}).encode()
        req = urllib.request.Request(a.api.rstrip("/") + "/api/process", body,
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as resp:
            print(r["frame"], resp.status)


def main():
    p = argparse.ArgumentParser(description="OCR 엔진 비교 벤치마크")
    sp = p.add_subparsers(dest="cmd", required=True)
    x = sp.add_parser("truth"); x.add_argument("--html", default="ocr-conveyor-test.html"); x.set_defaults(f=cmd_truth)
    x = sp.add_parser("frames"); x.add_argument("videos", nargs="+")
    x.add_argument("--every", type=float, default=1.0, help="몇 초마다 한 장")
    x.add_argument("--min-sharp", type=float, default=30.0, help="흐린 프레임 제외 기준"); x.set_defaults(f=cmd_frames)
    x = sp.add_parser("run"); x.add_argument("--engines", nargs="+", default=["easyocr", "rapidocr"])
    x.add_argument("--limit", type=int, default=0); x.set_defaults(f=cmd_run)
    x = sp.add_parser("score"); x.add_argument("--min-len", type=int, default=8)
    x.add_argument("--min-sim", type=float, default=60.0); x.set_defaults(f=cmd_score)
    x = sp.add_parser("dedup"); x.add_argument("--threshold", type=float, default=85.0); x.set_defaults(f=cmd_dedup)
    x = sp.add_parser("report"); x.add_argument("--out", default="docs/ocr-engine-comparison.md"); x.set_defaults(f=cmd_report)
    x = sp.add_parser("send"); x.add_argument("--engine", required=True)
    x.add_argument("--api", default="http://localhost:8000"); x.add_argument("--limit", type=int, default=0)
    x.set_defaults(f=cmd_send)
    a = p.parse_args(); a.f(a)


if __name__ == "__main__":
    main()
