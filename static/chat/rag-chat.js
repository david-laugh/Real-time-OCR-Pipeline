/*
 * <rag-chat> — question box that answers from stored OCR documents (POST /api/rag/search).
 *
 * Usage (dashboard):
 *   <script src="/static/chat/rag-chat.js" defer></script>
 *   <rag-chat></rag-chat>                      search all sessions
 *   <rag-chat session-id="ab23cd45"></rag-chat>  search one session only
 *
 * Attributes (all optional):
 *   api-base    Server origin for /api/rag/search. Default: location.origin
 *   session-id  Limit search to this session ID. Empty/absent = all sessions
 *   limit       Number of sources (1-20). Default: 5
 *
 * Events (bubble, composed):
 *   answer  detail: { query, sessionId, answer, sources, error }   error: null on success,
 *           otherwise a server code (invalid_request | rate_limited | search_failed | unavailable)
 *           or a client code (network | timeout | bad_response)
 *
 * Properties / methods: el.sessionId, el.ask(query), el.clear()
 */
"use strict";

(function () {
  if (customElements.get("rag-chat")) return;

  // ---- Settings ----------------------------------------------------------------
  const DEFAULT_LIMIT = 5;
  const REQUEST_TIMEOUT_MS = 30000;                              // Gemini answer can take a few seconds
  const MAX_QUERY_LENGTH = 1000;                                 // Same as the server
  const SESSION_ID_RE = /^[A-Za-z0-9_-]{1,64}$/;                // Same rule as the server

  const CLIENT_ERROR_MESSAGE = {
    network: "서버에 연결할 수 없어요. 주소와 네트워크를 확인해 주세요",
    timeout: "응답이 너무 오래 걸려요. 잠시 후 다시 해 주세요",
    bad_response: "서버 응답을 읽을 수 없어요. 잠시 후 다시 해 주세요",
  };

  // ---- Template ----------------------------------------------------------------
  const template = document.createElement("template");
  template.innerHTML = `
    <style>
      :host {
        --rag-chat-bg: #ffffff;
        --rag-chat-fg: #1f2328;
        --rag-chat-muted: #656d76;
        --rag-chat-border: #d0d7de;
        --rag-chat-user-bg: #ddf4ff;
        --rag-chat-error: #cf222e;
        --rag-chat-accent: #0969da;
        --rag-chat-height: 520px;
        display: block;
        font-family: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
        color: var(--rag-chat-fg);
      }
      @media (prefers-color-scheme: dark) {
        :host {
          --rag-chat-bg: #1c2027;
          --rag-chat-fg: #f1f3f5;
          --rag-chat-muted: #9aa3ad;
          --rag-chat-border: #3a414c;
          --rag-chat-user-bg: #12304a;
          --rag-chat-error: #ff8182;
          --rag-chat-accent: #58a6ff;
        }
      }
      [hidden] { display: none !important; }
      .card {
        display: flex;
        flex-direction: column;
        height: var(--rag-chat-height);
        background: var(--rag-chat-bg);
        border: 1px solid var(--rag-chat-border);
        border-radius: 12px;
        overflow: hidden;
      }
      header {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 8px;
        padding: 10px 14px;
        border-bottom: 1px solid var(--rag-chat-border);
        font-size: 13px;
      }
      header strong { font-size: 14px; }
      .scope { color: var(--rag-chat-muted); overflow-wrap: anywhere; text-align: right; }
      .log {
        flex: 1;
        overflow-y: auto;
        padding: 14px;
        display: flex;
        flex-direction: column;
        gap: 12px;
      }
      .empty { margin: auto; color: var(--rag-chat-muted); font-size: 13px; text-align: center; }
      .msg {
        max-width: 92%;
        padding: 10px 12px;
        border-radius: 10px;
        font-size: 14px;
        line-height: 1.55;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
      }
      .user { align-self: flex-end; background: var(--rag-chat-user-bg); }
      .bot { align-self: flex-start; border: 1px solid var(--rag-chat-border); }
      .bot.error { border-color: var(--rag-chat-error); color: var(--rag-chat-error); }
      .bot.pending { color: var(--rag-chat-muted); }
      .sources { margin: 8px 0 0; padding: 0; list-style: none; display: grid; gap: 6px; white-space: normal; }
      .sources li {
        padding: 6px 8px;
        border-radius: 6px;
        border: 1px solid var(--rag-chat-border);
        font-size: 12px;
        line-height: 1.45;
      }
      .src-head { display: flex; justify-content: space-between; gap: 8px; }
      .src-title { font-weight: 600; }
      .src-score { color: var(--rag-chat-muted); font-variant-numeric: tabular-nums; flex-shrink: 0; }
      .src-preview { color: var(--rag-chat-muted); margin-top: 2px; }
      form {
        display: flex;
        gap: 8px;
        padding: 10px;
        border-top: 1px solid var(--rag-chat-border);
      }
      textarea {
        flex: 1;
        min-height: 40px;
        max-height: 120px;
        resize: vertical;
        padding: 9px 10px;
        border: 1px solid var(--rag-chat-border);
        border-radius: 8px;
        background: transparent;
        color: inherit;
        font: inherit;
        font-size: 14px;
      }
      button {
        padding: 0 16px;
        border: 0;
        border-radius: 8px;
        background: var(--rag-chat-accent);
        color: #fff;
        font: inherit;
        font-size: 14px;
        cursor: pointer;
      }
      button:disabled { opacity: 0.5; cursor: default; }
    </style>
    <div class="card">
      <header>
        <strong>문서 질문</strong>
        <span class="scope" part="scope"></span>
      </header>
      <div class="log" role="log" aria-live="polite">
        <p class="empty">저장된 OCR 문서에 대해 질문해 보세요</p>
      </div>
      <form>
        <textarea rows="1" placeholder="질문 입력 (Enter 전송, Shift+Enter 줄바꿈)" aria-label="질문"></textarea>
        <button type="submit">보내기</button>
      </form>
    </div>
  `;

  // ---- Element -----------------------------------------------------------------
  class RagChat extends HTMLElement {
    static get observedAttributes() {
      return ["session-id"];
    }

    constructor() {
      super();
      this.attachShadow({ mode: "open" }).appendChild(template.content.cloneNode(true));
      const root = this.shadowRoot;
      this._log = root.querySelector(".log");
      this._empty = root.querySelector(".empty");
      this._scope = root.querySelector(".scope");
      this._form = root.querySelector("form");
      this._input = root.querySelector("textarea");
      this._button = root.querySelector("button");
      this._busy = false;

      this._input.maxLength = MAX_QUERY_LENGTH;
      this._form.addEventListener("submit", (e) => {
        e.preventDefault();
        this.ask(this._input.value);
      });
      this._input.addEventListener("keydown", (e) => {
        // isComposing: Enter that confirms Korean IME input must not send
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
          e.preventDefault();
          this.ask(this._input.value);
        }
      });
    }

    connectedCallback() {
      this._renderScope();
    }

    attributeChangedCallback() {
      this._renderScope();
    }

    get sessionId() {
      const id = (this.getAttribute("session-id") || "").trim();
      return id || null;
    }

    clear() {
      this._log.replaceChildren(this._empty);
      this._empty.hidden = false;
    }

    async ask(rawQuery) {
      const query = String(rawQuery || "").trim();
      if (!query || this._busy) return;
      const sessionId = this.sessionId;

      this._empty.hidden = true;
      this._input.value = "";
      this._append("user", query);
      const pending = this._append("bot pending", "답변을 찾는 중…");
      this._setBusy(true);

      const result = await this._request(query, sessionId);
      pending.remove();
      this._renderAnswer(result);
      this._setBusy(false);
      this._input.focus();
      this._emit("answer", { query, sessionId, ...result });
    }

    // ---- Internals -------------------------------------------------------------
    _apiBase() {
      return (this.getAttribute("api-base") || location.origin).replace(/\/+$/, "");
    }

    _limit() {
      const n = parseInt(this.getAttribute("limit"), 10);
      return Number.isFinite(n) ? Math.min(20, Math.max(1, n)) : DEFAULT_LIMIT;
    }

    async _request(query, sessionId) {
      const body = { query, limit: this._limit() };
      if (sessionId) body.session_id = sessionId;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      try {
        const res = await fetch(`${this._apiBase()}/api/rag/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        let data;
        try {
          data = await res.json();
        } catch (_) {
          return clientError("bad_response");
        }
        // Server always answers {answer, sources[, error]} (errors too, with a message in answer)
        if (!data || typeof data.answer !== "string") return clientError("bad_response");
        return {
          answer: data.answer,
          sources: Array.isArray(data.sources) ? data.sources : [],
          error: data.error || (res.ok ? null : "bad_response"),
        };
      } catch (err) {
        return clientError(err && err.name === "AbortError" ? "timeout" : "network");
      } finally {
        clearTimeout(timer);
      }
    }

    _renderAnswer({ answer, sources, error }) {
      const msg = this._append(error ? "bot error" : "bot", answer || "답변이 비어 있어요");
      if (!sources.length) return;
      const list = document.createElement("ol");
      list.className = "sources";
      sources.forEach((s, i) => {
        const li = document.createElement("li");
        const head = document.createElement("div");
        head.className = "src-head";
        const title = document.createElement("span");
        title.className = "src-title";
        // [n] matches the citation numbers in the answer (server numbers documents in this order)
        title.textContent = `[${i + 1}] ${s.title || "(제목 없음)"}`;
        const score = document.createElement("span");
        score.className = "src-score";
        score.textContent = typeof s.score === "number" ? s.score.toFixed(2) : "";
        head.append(title, score);
        const preview = document.createElement("div");
        preview.className = "src-preview";
        preview.textContent = s.preview || "";
        li.append(head, preview);
        list.appendChild(li);
      });
      msg.appendChild(list);
    }

    _append(className, text) {
      const div = document.createElement("div");
      div.className = `msg ${className}`;
      div.textContent = text;
      this._log.appendChild(div);
      this._log.scrollTop = this._log.scrollHeight;
      return div;
    }

    _setBusy(busy) {
      this._busy = busy;
      this._button.disabled = busy;
      this._input.readOnly = busy;
    }

    _renderScope() {
      const id = this.sessionId;
      if (id && !SESSION_ID_RE.test(id)) {
        this._scope.textContent = `잘못된 세션 ID: ${id}`;
      } else {
        this._scope.textContent = id ? `검색 범위: 세션 ${id}` : "검색 범위: 전체";
      }
    }

    _emit(type, detail) {
      this.dispatchEvent(new CustomEvent(type, { detail, bubbles: true, composed: true }));
    }
  }

  function clientError(code) {
    return { answer: CLIENT_ERROR_MESSAGE[code], sources: [], error: code };
  }

  customElements.define("rag-chat", RagChat);
})();
