"use strict";

// ---- Settings ------------------------------------------------------------------
const CAPTURE_INTERVAL_MS = 2000;     // Send interval, measured from the start of the previous send
const MAX_LONG_SIDE_PX = 1280;        // Max long side of a captured frame (never upscaled)
const JPEG_QUALITY = 0.8;
const REQUEST_TIMEOUT_MS = 8000;
const MAX_CONSECUTIVE_FAILURES = 3;   // Show "disconnected" after this many failures in a row
const FRAME_ENDPOINT = "/frame";
const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;
const DEBUG_REFRESH_MS = 500;

// ---- Elements ------------------------------------------------------------------
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
const debugEl = $("debug");

const canvas = document.createElement("canvas");
const ctx = canvas.getContext("2d");

// ---- State ---------------------------------------------------------------------
const ua = navigator.userAgent;
const isIOS = /iPhone|iPad|iPod/i.test(ua) ||
  (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
const isAndroid = /Android/i.test(ua);
const isInApp = /KAKAOTALK|NAVER\(inapp|Instagram|FBAN|FBAV|FB_IAB|Line\/|DaumApps|everytimeApp|; wv\)/i.test(ua);

const params = new URLSearchParams(location.search);
const sessionId = params.get("session") || "";
const DEBUG = params.get("debug") === "1";

let stream = null;
let running = false;       // User has pressed "start"
let loopGen = 0;           // Generation counter that invalidates an old loop after stop/restart
let loopTimer = null;
let failures = 0;
let sentCount = 0;
let wakeLock = null;

let orientation = null;          // "portrait" | "landscape", derived from the viewport
let skipNextCapture = false;     // Set on orientation change so a mid-rotation frame is not sent
let orientationChanges = 0;
let skippedCaptures = 0;
let videoResizes = 0;
let lastCapture = "-";

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

// ---- Orientation -----------------------------------------------------------------
// Set <html data-orientation> from the viewport in JS instead of relying only on
// the CSS orientation media query.
function updateOrientation() {
  const next = window.innerWidth > window.innerHeight ? "landscape" : "portrait";
  if (next === orientation) return;
  const isFirst = orientation === null;
  orientation = next;
  document.documentElement.dataset.orientation = next;
  if (!isFirst) {
    orientationChanges += 1;
    skipNextCapture = true;
  }
  renderDebug();
}

// ---- Debug overlay (?debug=1) ----------------------------------------------------
function renderDebug() {
  if (!DEBUG) return;
  const vv = window.visualViewport;
  const so = screen.orientation;
  const track = stream && stream.getVideoTracks()[0];
  const settings = track && track.getSettings ? track.getSettings() : {};
  const lines = [
    `viewport      ${window.innerWidth}×${window.innerHeight}`,
    `visualViewport ${vv ? `${Math.round(vv.width)}×${Math.round(vv.height)}` : "n/a"}`,
    `data-orient   ${orientation}`,
    `mq landscape  ${window.matchMedia("(orientation: landscape)").matches}`,
    `screen.orient ${so ? `${so.type} ${so.angle}°` : "n/a"}`,
    `window.orient ${typeof window.orientation === "number" ? `${window.orientation}°` : "n/a"}`,
    `video         ${video.videoWidth}×${video.videoHeight}`,
    `track         ${settings.width || "?"}×${settings.height || "?"}`,
    `last capture  ${lastCapture}`,
    `orient chg    ${orientationChanges}  skipped ${skippedCaptures}  video resize ${videoResizes}`,
  ];
  debugEl.textContent = lines.join("\n");
}

// ---- Error guidance ---------------------------------------------------------------
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

// ---- Camera --------------------------------------------------------------------
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
  // A track that ended in the background is reopened by onVisibilityChange
  if (!running || document.hidden) return;
  stopCapture();
  const err = new Error("track ended");
  err.name = "TrackEnded";
  showError(err);
}

// ---- Capture / upload ------------------------------------------------------------
function captureFrame() {
  // Read videoWidth/videoHeight on every capture so the canvas follows the current frame size
  const vw = video.videoWidth;
  const vh = video.videoHeight;
  if (!vw || !vh) return Promise.resolve(null);

  const scale = Math.min(1, MAX_LONG_SIDE_PX / Math.max(vw, vh));
  canvas.width = Math.round(vw * scale);
  canvas.height = Math.round(vh * scale);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  lastCapture = `${canvas.width}×${canvas.height} (video ${vw}×${vh}, ${orientation})`;
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
      // Bypasses the ngrok free-plan warning page; ignored by cloudflared
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

  let blob = null;
  if (skipNextCapture) {
    // The frame right after a rotation may still have the old size; skip it once
    skipNextCapture = false;
    skippedCaptures += 1;
    lastCapture = "skipped (orientation change)";
  } else {
    blob = await captureFrame();
    if (gen !== loopGen) return;
  }

  if (blob) {
    const ok = await uploadFrame(blob);   // A failed frame is dropped, not retried
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
  renderDebug();

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

// ---- Wake Lock -----------------------------------------------------------------
async function acquireWakeLock() {
  if (!("wakeLock" in navigator) || wakeLock) return;
  try {
    wakeLock = await navigator.wakeLock.request("screen");
    wakeLock.addEventListener("release", () => { wakeLock = null; });
  } catch (_) {
    wakeLock = null;   // Unsupported or denied: ignore
  }
}

function releaseWakeLock() {
  if (wakeLock) wakeLock.release().catch(() => {});
  wakeLock = null;
}

// ---- Start / stop --------------------------------------------------------------
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
  if (!running) {   // "stop" was pressed while the camera was starting
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
  // Back in foreground: iOS may have ended the track while hidden, so reopen it
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

// ---- Copy URL --------------------------------------------------------------------
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

// ---- Init ----------------------------------------------------------------------
function init() {
  setStatus("idle");
  setButtons();

  updateOrientation();
  window.addEventListener("resize", updateOrientation);
  window.addEventListener("orientationchange", updateOrientation);
  if (screen.orientation) screen.orientation.addEventListener("change", updateOrientation);

  if (DEBUG) {
    debugEl.hidden = false;
    video.addEventListener("resize", () => { videoResizes += 1; renderDebug(); });
    setInterval(renderDebug, DEBUG_REFRESH_MS);
    renderDebug();
  }

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
