"use strict";

// ---- 설정값 -----------------------------------------------------------------
const CAPTURE_INTERVAL_MS = 2000;     // 전송 간격 (이전 요청이 끝난 시점 기준으로 맞춤)
const MAX_LONG_SIDE_PX = 1280;        // 캡처 이미지의 긴 변 최대값 (확대하지 않음)
const JPEG_QUALITY = 0.8;
const REQUEST_TIMEOUT_MS = 8000;
const MAX_CONSECUTIVE_FAILURES = 3;   // 이 횟수만큼 연속 실패하면 "연결 끊김"
const FRAME_ENDPOINT = "/frame";
const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;

// ---- 요소 -------------------------------------------------------------------
const $ = (id) => document.getElementById(id);
const video = $("video");
const placeholder = $("placeholder");
const statusEl = $("status");
const sessionEl = $("session");
const startBtn = $("start");
const stopBtn = $("stop");
const statsEl = $("stats");
const errorBox = $("error");
const inappBox = $("inapp");

const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

// ---- 상태 -------------------------------------------------------------------
const ua = navigator.userAgent;
const isIOS = /iPhone|iPad|iPod/i.test(ua) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isAndroid = /Android/i.test(ua);
const isInApp = /KAKAOTALK|NAVER\(inapp|Instagram|FBAN|FBAV|FB_IAB|Line\/|DaumApps|everytimeApp|; wv\)/i.test(ua);

const sessionId = new URLSearchParams(location.search).get("session") || "";

let stream = null;
let running = false;       // 사용자가 "시작"을 누른 상태
let loopGen = 0;           // 정지/재시작 시 이전 루프를 무효화하기 위한 세대 번호
let loopTimer = null;
let failures = 0;
let sentCount = 0;
let wakeLock = null;

const STATUS_LABEL = {
  idle: "대기",
  starting: "카메라 준비 중",
  sending: "전송 중",
  paused: "일시 정지",
  disconnected: "연결 끊김",
  error: "오류",
};

function setStatus(state) {
  statusEl.dataset.state = state;
  statusEl.textContent = STATUS_LABEL[state];
}

function setButtons() {
  startBtn.disabled = running;
  stopBtn.disabled = !running;
}

// ---- 오류 안내 ----------------------------------------------------------------
function permissionSteps() {
  if (isIOS) {
    return [
      "주소창의 \"aA\" 버튼 → 웹사이트 설정 → 카메라 → \"허용\"",
      "또는 설정 앱 → Safari → 카메라 → \"허용\" 또는 \"확인\"",
      "변경 후 아래 \"다시 시도\"를 누르세요",
    ];
  }
  if (isAndroid) {
    return [
      "주소창 왼쪽의 자물쇠(또는 설정) 아이콘 → 권한 → 카메라 → \"허용\"",
      "목록에 없으면 Chrome 설정 → 사이트 설정 → 카메라에서 이 사이트 허용",
      "변경 후 아래 \"다시 시도\"를 누르세요",
    ];
  }
  return ["브라우저 주소창의 사이트 설정에서 카메라를 허용한 뒤 \"다시 시도\"를 누르세요"];
}

function describeError(err) {
  const name = err && err.name;
  if (name === "Unsupported") {
    if (!window.isSecureContext) {
      return {
        title: "HTTPS 주소로 접속해야 해요",
        message: "보안 연결(https://)이 아니면 브라우저가 카메라를 막아요. QR 코드의 https 주소로 다시 열어 주세요.",
      };
    }
    if (isInApp) {
      return {
        title: "이 앱 안에서는 카메라를 쓸 수 없어요",
        message: isIOS ? "Safari에서 열어 주세요." : "Chrome에서 열어 주세요.",
        steps: ["위 \"주소 복사\" 버튼으로 주소를 복사", "Safari / Chrome 주소창에 붙여넣기"],
      };
    }
    return {
      title: "카메라를 지원하지 않는 브라우저예요",
      message: "iPhone은 Safari, Android는 Chrome 최신 버전에서 열어 주세요.",
    };
  }
  if (name === "NotAllowedError" || name === "PermissionDeniedError" || name === "SecurityError") {
    return {
      title: "카메라 권한이 거부되었어요",
      message: "카메라 접근을 허용해야 촬영할 수 있어요.",
      steps: permissionSteps(),
    };
  }
  if (name === "NotFoundError" || name === "DevicesNotFoundError") {
    return {
      title: "카메라를 찾을 수 없어요",
      message: "이 기기에서 사용할 수 있는 카메라가 없어요.",
    };
  }
  if (name === "NotReadableError" || name === "TrackStartError") {
    return {
      title: "카메라를 사용할 수 없어요",
      message: "다른 앱(카메라, 영상통화 등)이나 다른 탭이 카메라를 쓰고 있을 수 있어요. 해당 앱을 닫고 다시 시도해 주세요.",
    };
  }
  if (name === "TrackEnded") {
    return {
      title: "카메라 연결이 끊겼어요",
      message: "다시 시도를 누르면 카메라를 다시 켜요.",
    };
  }
  return {
    title: "카메라를 시작하지 못했어요",
    message: (err && err.message) || "알 수 없는 오류가 발생했어요.",
  };
}

function showError(err, { retryable = true } = {}) {
  const info = describeError(err);
  $("error-title").textContent = info.title;
  $("error-message").textContent = info.message;
  const list = $("error-steps");
  list.replaceChildren(...(info.steps || []).map((text) => {
    const li = document.createElement("li");
    li.textContent = text;
    return li;
  }));
  list.hidden = !info.steps;
  $("retry").hidden = !retryable;
  errorBox.hidden = false;
  setStatus("error");
}

function hideError() {
  errorBox.hidden = true;
}

// ---- 카메라 -----------------------------------------------------------------
async function openCamera() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    const err = new Error("mediaDevices unavailable");
    err.name = "Unsupported";
    throw err;
  }
  const preferred = {
    audio: false,
    video: {
      facingMode: { ideal: "environment" },
      width: { ideal: 1920 },
      height: { ideal: 1080 },
    },
  };
  let s;
  try {
    s = await navigator.mediaDevices.getUserMedia(preferred);
  } catch (err) {
    if (err.name !== "OverconstrainedError" && err.name !== "ConstraintNotSatisfiedError") throw err;
    s = await navigator.mediaDevices.getUserMedia({ audio: false, video: true });
  }

  stream = s;
  stream.getVideoTracks().forEach((track) => {
    track.addEventListener("ended", onTrackEnded);
  });
  video.srcObject = stream;
  await video.play();
  placeholder.hidden = true;
}

function closeCamera() {
  if (stream) {
    stream.getTracks().forEach((track) => {
      track.removeEventListener("ended", onTrackEnded);
      track.stop();
    });
  }
  stream = null;
  video.srcObject = null;
  placeholder.hidden = false;
}

function cameraAlive() {
  return !!stream && stream.getVideoTracks().some((t) => t.readyState === "live");
}

function onTrackEnded() {
  // 백그라운드에서 끊긴 경우는 visibilitychange에서 다시 연다
  if (!running || document.hidden) return;
  stopCapture();
  const err = new Error("track ended");
  err.name = "TrackEnded";
  showError(err);
}

// ---- 캡처 / 전송 ----------------------------------------------------------------
function captureFrame() {
  // videoWidth/videoHeight는 현재 기기 방향이 반영된 값이라 가로/세로 모두 비율이 맞는다
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);

  const scale = Math.min(1, MAX_LONG_SIDE_PX / Math.max(vw, vh));
  canvas.width = Math.round(vw * scale);
  canvas.height = Math.round(vh * scale);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", JPEG_QUALITY));
}

async function uploadFrame(blob) {
  const body = new FormData();
  body.append("session_id", sessionId);
  body.append("frame", blob, "frame.jpg");

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const res = await fetch(FRAME_ENDPOINT, {
      method: "POST",
      body,
      signal: controller.signal,
      // ngrok 무료 플랜 경고 페이지 우회용 (cloudflared에서는 무시됨)
      headers: { "ngrok-skip-browser-warning": "1" },
    });
    return res.ok;
  } catch (_) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function tick(gen) {
  loopTimer = null;
  const startedAt = performance.now();

  const blob = await captureFrame();
  if (gen !== loopGen) return;
  if (blob) {
    const ok = await uploadFrame(blob);   // 실패한 프레임은 재전송하지 않고 버린다
    if (gen !== loopGen) return;
    const elapsed = Math.round(performance.now() - startedAt);
    if (ok) {
      failures = 0;
      sentCount += 1;
      setStatus("sending");
      statsEl.textContent = `보낸 프레임 ${sentCount}장 · 마지막 ${canvas.width}×${canvas.height}, ${elapsed}ms`;
    } else {
      failures += 1;
      if (failures >= MAX_CONSECUTIVE_FAILURES) setStatus("disconnected");
      statsEl.textContent = `전송 실패 ${failures}회 연속`;
    }
  }

  const wait = Math.max(0, CAPTURE_INTERVAL_MS - (performance.now() - startedAt));
  loopTimer = setTimeout(() => tick(gen), wait);
}

function startLoop() {
  stopLoop();
  const gen = loopGen;
  loopTimer = setTimeout(() => tick(gen), 0);
}

function stopLoop() {
  loopGen += 1;
  if (loopTimer !== null) clearTimeout(loopTimer);
  loopTimer = null;
}

// ---- Wake Lock ----------------------------------------------------------------
async function acquireWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch (_) {
    wakeLock = null;   // 미지원/거부 시 무시
  }
}

function releaseWakeLock() {
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

// ---- 시작 / 정지 ----------------------------------------------------------------
async function startCapture() {
  if (running) return;
  hideError();
  running = true;
  setButtons();
  setStatus("starting");
  try {
    await openCamera();
  } catch (err) {
    running = false;
    setButtons();
    closeCamera();
    showError(err);
    return;
  }
  if (!running) {   // 카메라 준비 중에 "정지"를 누른 경우
    closeCamera();
    return;
  }
  failures = 0;
  setStatus("sending");
  statsEl.textContent = "";
  acquireWakeLock();
  startLoop();
}

function stopCapture() {
  running = false;
  stopLoop();
  releaseWakeLock();
  closeCamera();
  setButtons();
  setStatus("idle");
}

async function onVisibilityChange() {
  if (!running) return;
  if (document.hidden) {
    stopLoop();
    setStatus("paused");
    return;
  }
  // 복귀: iOS 등에서 백그라운드 중 트랙이 끊겼으면 카메라를 다시 연다
  if (!cameraAlive()) {
    closeCamera();
    setStatus("starting");
    try {
      await openCamera();
    } catch (err) {
      stopCapture();
      showError(err);
      return;
    }
    if (!running) {
      closeCamera();
      return;
    }
  } else {
    video.play().catch(() => {});
  }
  if (!running || document.hidden) return;
  setStatus(failures >= MAX_CONSECUTIVE_FAILURES ? "disconnected" : "sending");
  acquireWakeLock();
  startLoop();
}

// ---- 주소 복사 ------------------------------------------------------------------
async function copyUrl() {
  const url = location.href;
  const btn = $("copy-url");
  let copied = false;
  try {
    await navigator.clipboard.writeText(url);
    copied = true;
  } catch (_) {
    const ta = document.createElement("textarea");
    ta.value = url;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
    ta.remove();
  }
  btn.textContent = copied ? "복사됨" : "복사 실패 – 주소창을 길게 눌러 복사";
}

// ---- 초기화 -----------------------------------------------------------------
function init() {
  setStatus("idle");
  setButtons();

  if (isInApp) inappBox.hidden = false;
  $("copy-url").addEventListener("click", copyUrl);

  if (!SESSION_ID_RE.test(sessionId)) {
    sessionEl.textContent = "세션 없음";
    startBtn.disabled = true;
    showError(
      { name: "NoSession", message: "QR 코드를 다시 스캔해서 열어 주세요." },
      { retryable: false },
    );
    $("error-title").textContent = "세션 정보가 없어요";
    return;
  }
  sessionEl.textContent = `세션 ${sessionId}`;

  startBtn.addEventListener("click", startCapture);
  stopBtn.addEventListener("click", stopCapture);
  $("retry").addEventListener("click", startCapture);
  document.addEventListener("visibilitychange", onVisibilityChange);
  window.addEventListener("pagehide", stopCapture);
}

init();
