/* static/app.js - Streaming Event Logic and UI updates */

document.addEventListener("DOMContentLoaded", () => {
  const form = document.getElementById("audit-form");
  const btnSubmit = document.getElementById("btn-submit");
  const statusDot = document.getElementById("status-dot");
  const statusText = document.getElementById("status-text");
  const iterBadge = document.getElementById("iter-badge");
  const scoreBadge = document.getElementById("score-badge");
  const consoleLogContainer = document.getElementById("console-log-container");
  const reportBody = document.getElementById("report-body");

  // Keep track of state during stream run
  let accumulatedMarkdown = "";
  let currentLogEntry = null;

  // Form submit handler
  form.addEventListener("submit", async (e) => {
    e.preventDefault();

    // Disable button, reset state
    btnSubmit.disabled = true;
    btnSubmit.innerHTML = `
      <svg style="width: 18px; height: 18px; animation: spin 1s linear infinite;" viewBox="0 0 24 24">
        <path fill="currentColor" d="M12 4V2C6.48 2 2 6.48 2 12h2c0-4.41 3.59-8 8-8zm0 14c-3.31 0-6-2.69-6-6H4c0 4.41 3.59 8 8 8v-2z"/>
      </svg>
      Auditing...
    `;

    // Clear logs and reset badges
    consoleLogContainer.innerHTML = "";
    reportBody.innerHTML = `<p style="color: var(--text-muted); font-style: italic;">Auditing started. Waiting for analyst agent to stream recommendations...</p>`;
    scoreBadge.style.display = "none";
    iterBadge.style.display = "none";
    accumulatedMarkdown = "";

    // Set connection status
    updateStatus("loading", "Processing");

    // Build request body from forms
    const payload = {
      objective: document.getElementById("input-objective").value,
      ontology_folder: document.getElementById("input-ontology-folder").value,
      dataset_folder: document.getElementById("input-dataset-folder").value,
      doc_db_path: document.getElementById("input-doc-db").value,
      mappings_db_path: document.getElementById("input-mappings-db").value,
      semantic_mapping_path: document.getElementById("input-semantic-mapping").value,
      output_md_path: document.getElementById("input-output-md").value,
      max_iterations: parseInt(document.getElementById("input-max-iterations").value),
      top_n: parseInt(document.getElementById("input-top-n").value),
      row_limit: parseInt(document.getElementById("input-row-limit").value),
      max_sql_iterations: parseInt(document.getElementById("input-max-sql-iterations").value),
      lines_threshold: parseInt(document.getElementById("input-lines-threshold").value),
      context_size_limit: parseInt(document.getElementById("input-context-size").value)
    };

    try {
      // POST request to stream endpoint
      const response = await fetch("/run", {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
      });

      if (!response.ok) {
        throw new Error(`HTTP Error ${response.status}: Failed to launch agent workflow.`);
      }

      // Start reading stream
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        
        // SSE messages are separated by double newline
        let parts = buffer.split("\n\n");
        // Keep the last part in buffer as it could be partial
        buffer = parts.pop();

        for (let part of parts) {
          part = part.trim();
          if (!part) continue;

          // Parse data line: starts with "data: "
          if (part.startsWith("data: ")) {
            const dataStr = part.substring(6).trim();
            try {
              const eventObj = JSON.parse(dataStr);
              handleEvent(eventObj);
            } catch (err) {
              console.error("JSON parsing error in chunk:", err, dataStr);
            }
          }
        }
      }
      
      // Handle remaining buffer
      if (buffer.trim().startsWith("data: ")) {
        const dataStr = buffer.trim().substring(6).trim();
        try {
          const eventObj = JSON.parse(dataStr);
          handleEvent(eventObj);
        } catch (err) {}
      }

    } catch (err) {
      appendLog("error", "System Error", `Execution encountered a failure: ${err.message}`);
      updateStatus("error", "Failed");
    } finally {
      btnSubmit.disabled = false;
      btnSubmit.innerHTML = `
        <svg style="width: 18px; height: 18px;" viewBox="0 0 24 24">
          <path fill="currentColor" d="M8 5v14l11-7z"/>
        </svg>
        Run Audit Workflow
      `;
    }
  });

  // Handle individual SSE events
  function handleEvent(eventObj) {
    const { event, data } = eventObj;
    const time = new Date().toLocaleTimeString();

    switch (event) {
      case "start":
        appendLog("info", "Workflow Started", `Audit objective initialized: "${data.objective}"`);
        break;

      case "iteration_start":
        iterBadge.textContent = `Iteration ${data.iteration}/${data.max_iterations}`;
        iterBadge.style.display = "inline-block";
        appendLog("info", `Iteration ${data.iteration}`, `Launching search iteration ${data.iteration}...`);
        break;

      case "planning_start":
        appendLog("planning", "Planner Active", `Decomposing audit objective for iteration ${data.iteration} into actions...`);
        break;

      case "planning_done":
        let stepsHtml = `<div style="margin-top: 4px;">Decomposed into ${data.action_plan.length} sub-queries:</div>`;
        stepsHtml += `<ul class="action-plan-list">`;
        data.action_plan.forEach((step, index) => {
          stepsHtml += `
            <li class="action-plan-item">
              <span class="plan-bullet"></span>
              <strong>Step ${index + 1} (${step.tool})</strong>: ${step.query_string}
            </li>
          `;
        });
        stepsHtml += `</ul>`;
        appendLog("planning", "Plan Generated", stepsHtml);
        break;

      case "sub_query_start":
        appendLog("query", `Query Step ${data.step_index + 1}`, `Executing sub-query targeting database:\nTool: "${data.tool}" | Query: "${data.query}"`);
        break;

      case "sub_query_done":
        const hasResult = data.result && data.result.trim();
        const detailsHtml = `
          <div>Completed execution of query: "${data.query}". Retrieved data payload:</div>
          <div class="log-details">${hasResult ? escapeHtml(data.result) : "(No records matching query)"}</div>
        `;
        appendLog("query", `Query Step ${data.step_index + 1} Result`, detailsHtml);
        break;

      case "analysis_start":
        appendLog("info", "Analyst Active", `Analyzing retrieved context. Streaming recommendations...`);
        // Prepare report render area
        reportBody.innerHTML = `<div class="markdown-body"></div>`;
        accumulatedMarkdown = "";
        break;

      case "analysis_chunk":
        accumulatedMarkdown += data.chunk;
        // Render markdown in real time
        if (typeof marked !== 'undefined') {
          reportBody.innerHTML = marked.parse(accumulatedMarkdown);
        } else {
          reportBody.textContent = accumulatedMarkdown;
        }
        break;

      case "analysis_done":
        appendLog("info", "Analysis Completed", `Recommending audit results based on compiled insights.`);
        break;

      case "evaluation_start":
        appendLog("eval", "Evaluation Active", `Quality Assurance Agent evaluating correctness and checking objective gaps...`);
        break;

      case "evaluation_done":
        scoreBadge.textContent = `QA Confidence: ${data.confidence_score}%`;
        scoreBadge.style.display = "inline-block";
        if (data.confidence_score >= 90) {
          scoreBadge.style.background = "rgba(16,185,129,0.15)";
          scoreBadge.style.borderColor = "rgba(16,185,129,0.3)";
          scoreBadge.style.color = "var(--success)";
        } else {
          scoreBadge.style.background = "rgba(245,158,11,0.15)";
          scoreBadge.style.borderColor = "rgba(245,158,11,0.3)";
          scoreBadge.style.color = "var(--warning)";
        }

        const evalHtml = `
          <div>QA Auditor gave confidence score: <strong>${data.confidence_score}/100</strong></div>
          <div style="margin-top: 6px; color: var(--text-secondary)">${data.explanation}</div>
        `;
        appendLog("eval", "Evaluation Verdict", evalHtml);
        break;

      case "loop_decision":
        if (data.loop_back) {
          appendLog("info", "Workflow Loop", `Confidence score ${data.confidence_score}% < 90%. Continuing to next iteration to close gaps...`);
        } else {
          appendLog("success", "Workflow Terminated", `Confidence score meets target threshold or loop has reached maximum limit.`);
        }
        break;

      case "final_report":
        updateStatus("active", "Completed");
        appendLog("success", "Report Saved", `Workflow fully finished. The final report is saved to path: "${data.result.report_path}"`);
        // Force complete render of final report
        if (typeof marked !== 'undefined') {
          reportBody.innerHTML = marked.parse(data.report);
        }
        break;

      case "error":
        appendLog("error", "Workflow Error", `Error: ${data.detail}`);
        updateStatus("error", "Failed");
        break;

      default:
        console.warn("Unknown event type:", event);
    }
  }

  // Update backend connectivity status indicators
  function updateStatus(dotClass, label) {
    statusDot.className = "status-dot";
    if (dotClass) {
      statusDot.classList.add(dotClass);
    }
    statusText.textContent = label;
  }

  // Helper to add log entries
  function appendLog(type, header, bodyHtml) {
    const entry = document.createElement("div");
    entry.className = `log-entry ${type}`;
    
    const time = new Date().toLocaleTimeString();
    entry.innerHTML = `
      <div class="log-header">
        <span>${header}</span>
        <span class="time">${time}</span>
      </div>
      <div>${bodyHtml}</div>
    `;

    consoleLogContainer.appendChild(entry);
    // Smooth scroll logs to bottom
    const consoleBody = document.getElementById("console-body");
    consoleBody.scrollTo({
      top: consoleBody.scrollHeight,
      behavior: 'smooth'
    });
  }

  // HTML escaping helper for safe display
  function escapeHtml(text) {
    const map = {
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;'
    };
    return text.replace(/[&<>"']/g, function(m) { return map[m]; });
  }
});

// Spin animation style injected if not present
const style = document.createElement('style');
style.innerHTML = `
  @keyframes spin {
    0% { transform: rotate(0deg); }
    100% { transform: rotate(360deg); }
  }
`;
document.head.appendChild(style);
