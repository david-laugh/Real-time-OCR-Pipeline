/*
 * <qr-pairing> — QR code that links a phone to the camera page, plus live connection status.
 *
 * Usage (dashboard):
 *   <script src="/static/pair/qr-pairing.js" defer></script>
 *   <qr-pairing></qr-pairing>
 *
 * Attributes (all optional):
 *   camera-url  URL template for the QR. Placeholders: {session}, {api}.
 *               Default: "{api}/camera?session={session}"
 *               GitHub Pages: "https://david-laugh.github.io/Real-time-OCR-Pipeline/?session={session}&api={api}"
 *   api-base    Server origin for the status API and {api}. Default: location.origin
 *   poll-ms     Status polling interval in ms. Default: 2000
 *
 * Events (bubble, composed):
 *   session-change  detail: { sessionId, cameraUrl }       fired on connect and on "new session"
 *   status-change   detail: { sessionId, state, status }   state: waiting | connected | disconnected | unreachable
 *
 * Properties / methods: el.sessionId, el.state, el.cameraUrl, el.newSession()
 */
"use strict";

(function () {
  if (customElements.get("qr-pairing")) return;

  // ---- Settings ----------------------------------------------------------------
  const SCRIPT_URL = document.currentScript ? document.currentScript.src : location.href;
  const QR_LIB_URL = new URL("../vendor/qrcode/qrcode.js?v=1.4.4", SCRIPT_URL).href;
  const DEFAULT_CAMERA_URL = "{api}/camera?session={session}";
  const DEFAULT_POLL_MS = 2000;
  const MIN_POLL_MS = 500;
  const REQUEST_TIMEOUT_MS = 5000;
  const SESSION_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz";   // 32 chars, no 0/o/1/l
  const SESSION_LENGTH = 8;
  const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;                // Same rule as the server
  const STORAGE_KEY = "qr-pairing.sessionId";
  const QUIET_ZONE = 4;                                          // Modules of white border

  const STATE_LABEL = {
    waiting: "대기 중",
    connected: "연결됨",
    disconnected: "연결 끊김",
    unreachable: "서버 응답 없음",
  };

  // ---- Helpers -----------------------------------------------------------------
  let qrLibPromise = null;
  function loadQrLib() {
    if (typeof window.qrcode === "function") return Promise.resolve(window.qrcode);
    if (!qrLibPromise) {
      qrLibPromise = new Promise((resolve, reject) => {
        const script = document.createElement("script");
        script.src = QR_LIB_URL;
        script.onload = () => resolve(window.qrcode);
        script.onerror = () => {
          qrLibPromise = null;
          reject(new Error("QR library failed to load"));
        };
        document.head.appendChild(script);
      });
    }
    return qrLibPromise;
  }

  function createSessionId() {
    const bytes = new Uint8Array(SESSION_LENGTH);
    crypto.getRandomValues(bytes);
    // 256 is a multiple of 32, so the modulo is unbiased
    return Array.from(bytes, (b) => SESSION_ALPHABET[b % SESSION_ALPHABET.length]).join("");
  }

  function readStoredId() {
    try {
      const id = sessionStorage.getItem(STORAGE_KEY);
      return id && SESSION_ID_RE.test(id) ? id : null;
    } catch (_) {
      return null;
    }
  }

  function storeId(id) {
    try { sessionStorage.setItem(STORAGE_KEY, id); } catch (_) { /* storage blocked: keep in memory */ }
  }

  function isLoopback(hostname) {
    return hostname === "localhost" || hostname.endsWith(".localhost") ||
      hostname === "127.0.0.1" || hostname === "[::1]" || hostname === "::1";
  }

  function buildQrSvg(qrcode, text) {
    const qr = qrcode(0, "M");   // 0 = pick the smallest version that fits
    qr.addData(text);
    qr.make();
    const count = qr.getModuleCount();
    const size = count + QUIET_ZONE * 2;
    let d = "";
    for (let row = 0; row < count; row += 1) {
      for (let col = 0; col < count; col += 1) {
        if (qr.isDark(row, col)) d += `M${col + QUIET_ZONE} ${row + QUIET_ZONE}h1v1h-1z`;
      }
    }
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
    svg.setAttribute("shape-rendering", "crispEdges");
    const bg = document.createElementNS(NS, "rect");
    bg.setAttribute("width", String(size));
    bg.setAttribute("height", String(size));
    bg.setAttribute("fill", "#fff");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    path.setAttribute("fill", "#000");
    svg.append(bg, path);
    return svg;
  }

  // ---- Template ----------------------------------------------------------------
  const template = document.createElement("template");
  template.innerHTML = `
    <style>
      :host {
        --qr-pairing-bg: #ffffff;
        --qr-pairing-fg: #1f2328;
        --qr-pairing-muted: #656d76;
        --qr-pairing-border: #d0d7de;
        --qr-pairing-size: 240px;
        display: block;
        font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
        color: var(--qr-pairing-fg);
      }
      @media (prefers-color-scheme: dark) {
        :host {
          --qr-pairing-bg: #1c2027;
          --qr-pairing-fg: #f1f3f5;
          --qr-pairing-muted: #9aa3ad;
          --qr-pairing-border: #3a414c;
        }
      }
      [hidden] { display: none !important; }
      .card {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 10px;
        padding: 16px;
        border: 1px solid var(--qr-pairing-border);
        border-radius: 12px;
        background: var(--qr-pairing-bg);
        text-align: center;
      }
      .head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        width: 100%;
      }
      .title { font-weight: 700; }
      .badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 14px;
        font-weight: 600;
      }
      .badge::before {
        content: "";
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: #9aa3ad;
      }
      .badge[data-state="connected"]::before { background: #22c55e; }
      .badge[data-state="disconnected"]::before { background: #ef4444; }
      .badge[data-state="unreachable"]::before { background: #f59e0b; }
      .qr {
        width: var(--qr-pairing-size);
        max-width: 100%;
        aspect-ratio: 1;
        border-radius: 8px;
        overflow: hidden;
        background: #fff;
      }
      .qr svg { display: block; width: 100%; height: 100%; }
      .qr-error {
        display: flex;
        align-items: center;
        justify-content: center;
        height: 100%;
        padding: 12px;
        color: #b42318;
        font-size: 13px;
      }
      .warn {
        width: 100%;
        box-sizing: border-box;
        padding: 8px 10px;
        border-radius: 8px;
        background: rgba(245, 158, 11, 0.14);
        border: 1px solid rgba(245, 158, 11, 0.5);
        font-size: 13px;
        line-height: 1.45;
        text-align: left;
      }
      .detail { margin: 0; font-size: 14px; color: var(--qr-pairing-muted); min-height: 1.2em; }
      .sid { font-size: 13px; color: var(--qr-pairing-muted); }
      .sid b { font-family: ui-monospace, Menlo, monospace; color: var(--qr-pairing-fg); letter-spacing: 0.05em; }
      .url {
        max-width: 100%;
        font-size: 12px;
        color: var(--qr-pairing-muted);
        overflow-wrap: anywhere;
      }
      button {
        min-height: 36px;
        padding: 0 16px;
        border: 1px solid var(--qr-pairing-border);
        border-radius: 8px;
        background: transparent;
        color: inherit;
        font: inherit;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
      }
    </style>
    <div class="card">
      <div class="head">
        <span class="title">휴대폰 연결</span>
        <span class="badge" data-state="waiting">대기 중</span>
      </div>
      <div class="warn" hidden></div>
      <div class="qr" role="img" aria-label="카메라 페이지로 연결되는 QR 코드"></div>
      <p class="detail"></p>
      <span class="sid">세션 <b></b></span>
      <a class="url" target="_blank" rel="noopener"></a>
      <button type="button" class="new">새 세션</button>
    </div>
  `;

  // ---- Element -----------------------------------------------------------------
  class QrPairing extends HTMLElement {
    static get observedAttributes() {
      return ["camera-url", "api-base", "poll-ms"];
    }

    constructor() {
      super();
      this.attachShadow({ mode: "open" }).appendChild(template.content.cloneNode(true));
      const $ = (sel) => this.shadowRoot.querySelector(sel);
      this._els = {
        badge: $(".badge"),
        warn: $(".warn"),
        qr: $(".qr"),
        detail: $(".detail"),
        sid: $(".sid b"),
        url: $(".url"),
        newBtn: $(".new"),
      };
      this._els.newBtn.addEventListener("click", () => this.newSession());
      this._sessionId = null;
      this._state = null;
      this._pollTimer = null;
      this._pollGen = 0;
    }

    connectedCallback() {
      if (!this._sessionId) {
        this._sessionId = readStoredId() || createSessionId();
        storeId(this._sessionId);
      }
      this._refresh();
    }

    disconnectedCallback() {
      this._stopPolling();
    }

    attributeChangedCallback() {
      if (this.isConnected && this._sessionId) this._refresh();
    }

    get sessionId() { return this._sessionId; }
    get state() { return this._state; }

    get cameraUrl() {
      const template = (this.getAttribute("camera-url") || "").trim() || DEFAULT_CAMERA_URL;
      return template.split("{session}").join(this._sessionId || "").split("{api}").join(this._apiBase());
    }

    newSession() {
      this._sessionId = createSessionId();
      storeId(this._sessionId);
      this._refresh();
    }

    // ---- Internal ----------------------------------------------------------------
    _apiBase() {
      const raw = (this.getAttribute("api-base") || "").trim();
      return (raw || location.origin).replace(/\/+$/, "");
    }

    _pollMs() {
      const n = Number(this.getAttribute("poll-ms"));
      return Number.isFinite(n) && n >= MIN_POLL_MS ? n : DEFAULT_POLL_MS;
    }

    _refresh() {
      const url = this.cameraUrl;
      this._state = null;
      this._els.sid.textContent = this._sessionId;
      this._els.url.textContent = url;
      this._els.url.href = url;
      this._renderWarning();
      this._renderQr(url);
      this._setStatus("waiting", null);
      this._emit("session-change", { sessionId: this._sessionId, cameraUrl: url });
      this._startPolling();
    }

    _renderWarning() {
      let text = "";
      let server;
      try {
        server = new URL(this._apiBase());
      } catch (_) {
        server = null;
      }
      if (!server) {
        text = "api-base 주소 형식이 올바르지 않아요.";
      } else if (isLoopback(server.hostname)) {
        text = "지금 localhost로 열려 있어서 휴대폰이 이 주소에 접속할 수 없어요. " +
          "터널 주소(https://…)로 이 화면을 다시 열어 주세요.";
      } else if (server.protocol !== "https:") {
        text = "https가 아닌 주소예요. 휴대폰이 접속은 되더라도 카메라가 막혀요. " +
          "터널 주소(https://…)로 이 화면을 다시 열어 주세요.";
      }
      this._els.warn.textContent = text;
      this._els.warn.hidden = !text;
    }

    _renderQr(url) {
      const box = this._els.qr;
      loadQrLib()
        .then((qrcode) => {
          if (url !== this.cameraUrl) return;   // Superseded by a newer session/attribute
          box.replaceChildren(buildQrSvg(qrcode, url));
        })
        .catch(() => {
          const msg = document.createElement("div");
          msg.className = "qr-error";
          msg.textContent = "QR 라이브러리를 불러오지 못했어요.";
          box.replaceChildren(msg);
        });
    }

    _startPolling() {
      this._stopPolling();
      const gen = this._pollGen;
      this._poll(gen);
    }

    _stopPolling() {
      this._pollGen += 1;
      if (this._pollTimer !== null) clearTimeout(this._pollTimer);
      this._pollTimer = null;
    }

    async _poll(gen) {
      this._pollTimer = null;
      const sessionId = this._sessionId;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      let state = "unreachable";
      let status = null;
      try {
        const res = await fetch(`${this._apiBase()}/session/${encodeURIComponent(sessionId)}/status`, {
          cache: "no-store",
          signal: controller.signal,
        });
        if (res.ok) {
          status = await res.json();
          state = status.state;
        }
      } catch (_) {
        // Network error or timeout: report as unreachable
      } finally {
        clearTimeout(timer);
      }
      if (gen !== this._pollGen) return;
      this._setStatus(state, status);
      this._pollTimer = setTimeout(() => this._poll(gen), this._pollMs());
    }

    _setStatus(state, status) {
      const { badge, detail } = this._els;
      badge.dataset.state = state;
      badge.textContent = STATE_LABEL[state] || state;

      if (state === "connected") {
        detail.textContent = `프레임 ${status.frames_received}장 수신 · 마지막 ${status.seconds_since_last}초 전`;
      } else if (state === "disconnected") {
        detail.textContent = `마지막 프레임 ${Math.round(status.seconds_since_last)}초 전 · 휴대폰에서 촬영이 멈췄어요`;
      } else if (state === "unreachable") {
        detail.textContent = `상태 API에 연결할 수 없어요 (${this._apiBase()})`;
      } else {
        detail.textContent = "휴대폰으로 QR을 스캔한 뒤 \"시작\"을 눌러 주세요";
      }

      if (state !== this._state) {
        this._state = state;
        this._emit("status-change", { sessionId: this._sessionId, state, status });
      }
    }

    _emit(type, detail) {
      this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
    }
  }

  customElements.define("qr-pairing", QrPairing);
})();
