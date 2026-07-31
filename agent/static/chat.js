// Chat streaming client.
//
// Reads newline-delimited JSON from a streamed POST response. No framework and no build
// step: this file plus the template is the whole chat UI.
//
// The stream carries progress events as well as the reply, because a routine request
// takes tens of seconds of model calls and database searches. Showing each step is both
// a progress indicator and the evidence that the coach read the profile and searched the
// exercise database rather than answering from memory.

(function () {
  "use strict";

  var conversation = document.querySelector(".conversation");
  var transcript = document.getElementById("transcript");
  var composer = document.getElementById("composer");
  var input = document.getElementById("message");
  var send = document.getElementById("send");
  if (!conversation || !composer) return;

  var sessionId = conversation.dataset.sessionId;
  var streaming = false;

  // Pipeline stages worth keeping in the scrollback afterwards.
  var MILESTONES = ["tool_result", "validated", "critiqued", "escalated", "written"];

  function atBottom() {
    // Only auto-scroll when already following along, so scrolling back to re-read
    // something isn't yanked away by the next event.
    return transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 80;
  }

  function scroll(wasAtBottom) {
    if (wasAtBottom) transcript.scrollTop = transcript.scrollHeight;
  }

  function clearEmptyState() {
    var empty = transcript.querySelector(".empty-chat");
    if (empty) empty.remove();
  }

  function addMessage(role, text) {
    var wasAtBottom = atBottom();
    clearEmptyState();
    var wrap = document.createElement("div");
    wrap.className = "msg " + role;
    var bubble = document.createElement("div");
    bubble.className = "bubble";
    // textContent, not innerHTML: model output is untrusted input as far as the DOM is
    // concerned, and the transcript is deliberately plain text.
    bubble.textContent = text;
    wrap.appendChild(bubble);
    transcript.appendChild(wrap);
    scroll(wasAtBottom);
    return bubble;
  }

  function addTrace(text, className) {
    var wasAtBottom = atBottom();
    clearEmptyState();
    var line = document.createElement("div");
    line.className = "trace" + (className ? " " + className : "");
    line.textContent = text;
    transcript.appendChild(line);
    scroll(wasAtBottom);
    return line;
  }

  var working = null;

  function setWorking(text) {
    if (!working) {
      working = addTrace(text, "working");
    } else {
      working.textContent = text;
      scroll(atBottom());
    }
  }

  function clearWorking() {
    if (working) {
      working.remove();
      working = null;
    }
  }

  function handle(event) {
    switch (event.event) {
      case "accepted":
        setWorking("Thinking…");
        break;
      case "ping":
        // Keepalive only. Deliberately silent: a visible tick would read as progress
        // when nothing has happened.
        break;
      case "notice":
        addTrace(event.text, "notice");
        break;
      case "tool_call":
        setWorking(event.summary || "Working…");
        break;
      case "tool_result":
        clearWorking();
        addTrace(event.summary || "");
        setWorking("Thinking…");
        break;
      case "routine_started":
        clearWorking();
        addTrace(
          "Building a full program. This runs the drafting, checking and review passes — " +
            "expect tens of seconds.",
          "notice"
        );
        setWorking("Starting…");
        break;
      case "routine_progress":
        // Milestones stay in the transcript; intermediate chatter is transient, so it
        // overwrites one working line instead of filling the scrollback.
        if (MILESTONES.indexOf(event.stage) !== -1) {
          clearWorking();
          addTrace(event.text || "");
          setWorking("Working…");
        } else {
          setWorking(event.text || event.stage || "Working…");
        }
        break;
      case "routine_finished":
        clearWorking();
        if (event.ok && event.url) {
          var line = addTrace("");
          line.className = "trace result";
          line.appendChild(document.createTextNode("Saved “" + (event.name || "routine") + "” — "));
          var link = document.createElement("a");
          link.href = event.url;
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent = "open it in the training app";
          line.appendChild(link);
        } else if (event.ok) {
          addTrace("Program drafted and checked, not saved (draft only).", "result");
        } else {
          addTrace("Program not saved: " + (event.error || "unknown reason"), "failed");
        }
        setWorking("Writing up…");
        break;
      case "assistant":
        clearWorking();
        if (event.content) addMessage("assistant", event.content);
        break;
      case "error":
        clearWorking();
        addMessage("error", event.message || "Something went wrong.");
        break;
      case "done":
        clearWorking();
        break;
    }
  }

  function setBusy(busy) {
    streaming = busy;
    send.disabled = busy;
    input.disabled = busy;
    send.textContent = busy ? "Working…" : "Send";
    if (!busy) input.focus();
  }

  async function submit(text) {
    setBusy(true);
    addMessage("user", text);
    try {
      var response = await fetch("/chat/" + sessionId + "/message", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      if (!response.ok || !response.body) {
        var detail = "";
        try {
          detail = (await response.json()).error || "";
        } catch (e) {
          detail = response.status + " " + response.statusText;
        }
        clearWorking();
        addMessage("error", "Request failed: " + detail);
        return;
      }

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        // A chunk boundary can fall mid-line, so the tail is kept until its newline
        // arrives rather than parsed as a truncated object.
        var lines = buffer.split("\n");
        buffer = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          var line = lines[i].trim();
          if (!line) continue;
          try {
            handle(JSON.parse(line));
          } catch (e) {
            // One malformed event must not abort a stream that is otherwise fine.
            console.warn("unparseable stream line", line, e);
          }
        }
      }
    } catch (err) {
      clearWorking();
      addMessage("error", "Connection lost: " + err.message);
    } finally {
      setBusy(false);
      // The server titles a session from its first message, but the sidebar was
      // rendered before that message existed.
      var titleEl = document.querySelector(".session.active .session-title");
      if (titleEl && titleEl.textContent.trim() === "New conversation") {
        titleEl.textContent = text.slice(0, 60);
      }
    }
  }

  composer.addEventListener("submit", function (event) {
    event.preventDefault();
    if (streaming) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    submit(text);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      composer.requestSubmit();
    }
  });

  transcript.scrollTop = transcript.scrollHeight;
  input.focus();
})();
