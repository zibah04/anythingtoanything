import { convertLocally } from "./local.js";
import { applicationBase } from "./paths.js";

const appBase = applicationBase(import.meta.url);

const $ = (selector) => document.querySelector(selector);
const queue = [];
let configuration,
  busy = false,
  toastTimer;
const aliases = { jpeg: "jpg", tif: "tiff", htm: "html", markdown: "md" };
const elements = {
  input: $("#file-input"),
  drop: $("#dropzone"),
  list: $("#file-list"),
  convert: $("#convert-button"),
  label: $("#convert-label"),
  caption: $("#conversion-caption"),
  actions: $("#queue-actions"),
  count: $("#queue-count"),
  clear: $("#clear-files"),
  empty: $("#empty-format"),
  error: $("#connection-error"),
  catalog: $("#format-catalog"),
};
function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.classList.add("icon");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "20");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", "#i-" + name);
  svg.append(use);
  return svg;
}
function announce(message) {
  $("#announcements").textContent = message;
}
function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    element.hidden = true;
  }, 6500);
}
function sizeLabel(size) {
  return size < 1024 * 1024
    ? `${Math.max(1, Math.round(size / 1024))} KB`
    : `${(size / 1024 / 1024).toFixed(1)} MB`;
}
function targetInfo(item) {
  return item.format.targets.find((format) => format.extension === item.target);
}
function release(item) {
  if (item.url) {
    URL.revokeObjectURL(item.url);
    item.url = undefined;
  }
}

async function loadConfiguration() {
  elements.error.hidden = true;
  try {
    const response = await fetch(new URL("api/formats", appBase), {
      signal: AbortSignal.timeout(12000),
    });
    if (!response.ok) throw new Error();
    configuration = await response.json();
    elements.input.accept = configuration.formats
      .map((format) => "." + format.extension)
      .concat(Object.keys(aliases).map((extension) => "." + extension))
      .join(",");
    elements.drop.disabled = false;
    renderCatalog();
  } catch {
    $("#connection-message").textContent =
      "Couldn’t connect to the converter. Check the server and try again.";
    elements.error.hidden = false;
    elements.drop.disabled = true;
  }
}

function defaultTarget(format) {
  const preferred =
    format.family === "Images"
      ? format.extension === "png"
        ? "jpg"
        : "png"
      : format.family === "Spreadsheets"
        ? format.extension === "csv"
          ? "xlsx"
          : "csv"
        : format.extension === "pdf"
          ? "docx"
          : "pdf";
  return format.targets.some((target) => target.extension === preferred)
    ? preferred
    : format.targets[0].extension;
}

function addFiles(files) {
  if (!configuration) return;
  if (busy) {
    toast("Wait for the current batch to finish before adding more files.");
    return;
  }
  let added = 0;
  const errors = [];
  for (const file of files) {
    if (queue.length >= configuration.max_batch_size) {
      errors.push("A batch can contain up to 10 files.");
      break;
    }
    const extension = file.name.includes(".")
      ? file.name.split(".").pop().toLowerCase()
      : "";
    const source = aliases[extension] || extension;
    const format = configuration.formats.find(
      (format) => format.extension === source,
    );
    if (!format) {
      errors.push(
        `${file.name}: unsupported format. Explore formats to see what works.`,
      );
      continue;
    }
    if (!file.size) {
      errors.push(`${file.name}: this file is empty.`);
      continue;
    }
    if (file.size > configuration.max_file_size) {
      errors.push(`${file.name}: files must be 25 MB or smaller.`);
      continue;
    }
    if (
      queue.some(
        (item) =>
          item.file.name === file.name &&
          item.file.size === file.size &&
          item.file.lastModified === file.lastModified,
      )
    ) {
      errors.push(`${file.name} is already in the queue.`);
      continue;
    }
    queue.push({
      id: crypto.randomUUID(),
      file,
      source,
      format,
      target: defaultTarget(format),
      status: "pending",
      pdfMode: "precise",
    });
    added++;
  }
  elements.input.value = "";
  renderQueue();
  if (errors.length)
    toast(
      errors.length === 1
        ? errors[0]
        : `${errors[0]} (${errors.length} files weren’t added.)`,
      true,
    );
  if (added)
    announce(
      `${added} ${added === 1 ? "file added" : "files added"} to the conversion queue.`,
    );
}

function renderQueue() {
  elements.list.setAttribute("aria-busy", String(busy));
  elements.list.replaceChildren();
  for (const item of queue) {
    const row = node("article", "file-row");
    const top = node("div", "file-top");
    const symbol = node("div", "file-symbol");
    symbol.append(icon("file"));
    const info = node("div", "file-info");
    const name = node("div", "file-name", item.file.name);
    name.title = item.file.name;
    info.append(
      name,
      node(
        "div",
        "file-meta",
        `${item.source.toUpperCase()} · ${sizeLabel(item.file.size)}`,
      ),
    );
    const remove = node("button", "icon-button");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${item.file.name}`);
    remove.append(icon("close"));
    remove.disabled = busy;
    remove.addEventListener("click", () => {
      release(item);
      queue.splice(queue.indexOf(item), 1);
      renderQueue();
      announce("File removed.");
    });
    top.append(symbol, info, remove);
    row.append(top);
    const controls = node("div", "file-controls");
    const label = node("label", "", "Convert to");
    label.htmlFor = "target-" + item.id;
    const select = node("select", "target-select");
    select.id = label.htmlFor;
    select.setAttribute("aria-label", `Output format for ${item.file.name}`);
    select.disabled = busy;
    for (const target of item.format.targets) {
      const option = node(
        "option",
        "",
        `${target.extension.toUpperCase()} — ${target.label}`,
      );
      option.value = target.extension;
      option.selected = item.target === target.extension;
      select.append(option);
    }
    select.addEventListener("change", () => {
      release(item);
      item.target = select.value;
      item.status = "pending";
      item.error = undefined;
      renderQueue();
      document.getElementById(select.id)?.focus();
    });
    const route = targetInfo(item);
    const badge = node(
      "span",
      "route-badge" + (route.engine === "server" ? " server" : ""),
    );
    badge.append(
      icon(route.engine === "browser" ? "bolt" : "shield"),
      document.createTextNode(
        route.engine === "browser" ? "On device" : "Server",
      ),
    );
    badge.title =
      route.engine === "browser"
        ? "This conversion stays in your browser."
        : "Processed temporarily on the server.";
    controls.append(label, select, badge);
    row.append(controls);
    let conversionNote = route.note;
    if (
      item.source === "pdf" &&
      item.target === "docx" &&
      configuration.pdf_word_modes?.length
    ) {
      const options = node("div", "pdf-options");
      const modeLabel = node("label", "pdf-mode-label", "Word conversion");
      modeLabel.htmlFor = "pdf-mode-" + item.id;
      const modeSelect = node("select", "target-select pdf-mode-select");
      modeSelect.id = modeLabel.htmlFor;
      modeSelect.disabled = busy;
      modeSelect.setAttribute(
        "aria-label",
        `Word conversion mode for ${item.file.name}`,
      );
      for (const mode of configuration.pdf_word_modes) {
        const option = node("option", "", mode.label);
        option.value = mode.value;
        option.selected = item.pdfMode === mode.value;
        modeSelect.append(option);
      }
      modeSelect.addEventListener("change", () => {
        release(item);
        item.pdfMode = modeSelect.value;
        item.status = "pending";
        item.error = undefined;
        renderQueue();
        document.getElementById(modeSelect.id)?.focus();
      });
      options.append(modeLabel, modeSelect);
      row.append(options);
      conversionNote =
        configuration.pdf_word_modes.find((mode) => mode.value === item.pdfMode)
          ?.note ?? route.note;
    }
    if (conversionNote) {
      const note = node("p", "file-note");
      note.append(icon("info"), document.createTextNode(conversionNote));
      row.append(note);
    }
    if (item.status !== "pending") {
      const status = node(
        "div",
        "file-status" + (item.status === "error" ? " error" : ""),
      );
      if (item.status === "converting") {
        const text = node("span");
        text.append(
          node("span", "spinner"),
          document.createTextNode("Converting…"),
        );
        status.append(text);
      } else if (item.status === "queued")
        status.append(node("span", "", "Waiting for a conversion slot…"));
      else if (item.status === "done") {
        const success = node(
          "span",
          "",
          `Ready · ${sizeLabel(item.outputSize)}`,
        );
        const download = node("a", "download-button");
        download.href = item.url;
        download.download = item.outputName;
        download.append(
          icon("download"),
          document.createTextNode("Download " + item.target.toUpperCase()),
        );
        download.setAttribute("aria-label", `Download ${item.outputName}`);
        status.append(success, download);
      } else {
        const retry = node("button", "retry-button", "Retry");
        retry.type = "button";
        retry.disabled = busy;
        retry.addEventListener("click", () => runBatch([item]));
        status.append(node("span", "", item.error), retry);
      }
      row.append(status);
    }
    elements.list.append(row);
  }
  elements.empty.hidden = queue.length > 0;
  elements.actions.hidden = !queue.length;
  elements.count.textContent = `${queue.length} of 10 ${queue.length === 1 ? "file" : "files"}`;
  elements.clear.disabled = busy;
  elements.drop.disabled = busy || !configuration;
  const remaining = queue.filter((item) => item.status !== "done").length;
  const completed = queue.filter((item) => item.status === "done").length;
  elements.convert.disabled = busy || remaining === 0;
  elements.label.textContent = busy
    ? `Converting · ${completed} of ${queue.length} ready`
    : queue.length && !remaining
      ? "All files converted"
      : remaining
        ? `Convert ${remaining} ${remaining === 1 ? "file" : "files"}`
        : "Convert files";
  elements.caption.textContent = busy
    ? "Keep this tab open. Your files are on their way."
    : queue.length && !remaining
      ? "All done. Your files are ready to download."
      : "A fresh format is just a file away.";
}

async function convertFile(item) {
  item.status = "converting";
  item.error = undefined;
  renderQueue();
  try {
    let blob;
    if (targetInfo(item).engine === "browser")
      blob = await convertLocally(item.file, item.source, item.target);
    else {
      const data = new FormData();
      data.append("file", item.file);
      data.append("target", item.target);
      if (item.source === "pdf" && item.target === "docx")
        data.append("pdf_mode", item.pdfMode);
      let response;
      try {
        response = await fetch(new URL("api/convert", appBase), {
          method: "POST",
          body: data,
          signal: AbortSignal.timeout(120000),
        });
      } catch (error) {
        throw new Error(
          error.name === "TimeoutError"
            ? "The conversion timed out. Try a smaller file."
            : "Couldn’t reach the server. Check your connection and retry.",
        );
      }
      if (!response.ok) {
        let message =
          "The conversion failed. Please try another file or format.";
        try {
          const data = await response.json();
          if (typeof data.detail === "string") message = data.detail;
        } catch {
          /* A proxy may return a non-JSON error. */
        }
        throw new Error(message);
      }
      blob = await response.blob();
    }
    if (!blob.size) throw new Error("The conversion produced an empty file.");
    release(item);
    item.url = URL.createObjectURL(blob);
    item.outputSize = blob.size;
    item.outputName =
      item.file.name.replace(/\.[^.]+$/, "") + "." + item.target;
    item.status = "done";
  } catch (error) {
    item.status = "error";
    item.error = error.message || "This file could not be converted.";
  }
  renderQueue();
}

async function runBatch(
  selected = queue.filter((item) => item.status !== "done"),
) {
  if (busy || !selected.length) return;
  busy = true;
  selected.forEach((item) => {
    item.status = "queued";
  });
  renderQueue();
  async function pool(items, capacity) {
    let cursor = 0;
    async function worker() {
      while (cursor < items.length) await convertFile(items[cursor++]);
    }
    await Promise.all(
      Array.from({ length: Math.min(capacity, items.length) }, worker),
    );
  }
  try {
    await Promise.all([
      pool(
        selected.filter((item) => targetInfo(item).engine === "browser"),
        2,
      ),
      pool(
        selected.filter((item) => targetInfo(item).engine === "server"),
        Math.min(2, configuration.server_conversion_workers ?? 2),
      ),
    ]);
  } finally {
    busy = false;
    renderQueue();
  }
  const done = selected.filter((item) => item.status === "done").length;
  const message =
    done === selected.length
      ? `${done} ${done === 1 ? "file is" : "files are"} ready. Download below.`
      : `${done} of ${selected.length} files converted. Check the messages beside the other files.`;
  announce(message);
  toast(message, done !== selected.length);
}

function renderCatalog() {
  if (!configuration) return;
  const search = $("#format-search").value.trim().toLowerCase();
  elements.catalog.replaceChildren();
  const groups = new Map();
  for (const format of configuration.formats) {
    if (
      search &&
      !`${format.extension} ${format.label} ${format.family}`
        .toLowerCase()
        .includes(search)
    )
      continue;
    if (!groups.has(format.family)) groups.set(format.family, []);
    groups.get(format.family).push(format);
  }
  for (const [family, formats] of groups) {
    const group = node("div", "catalog-group");
    group.append(node("h3", "", family));
    for (const format of formats) {
      const row = node("div", "catalog-row");
      row.append(
        node("span", "catalog-source", format.extension.toUpperCase() + " →"),
      );
      const targets = node("div", "catalog-targets");
      for (const target of format.targets) {
        const chip = node(
          "span",
          "catalog-chip" + (target.engine === "browser" ? " local" : ""),
          target.extension.toUpperCase() +
            (target.engine === "browser" ? " · On device" : ""),
        );
        chip.title = target.note || target.label;
        targets.append(chip);
      }
      row.append(targets);
      group.append(row);
    }
    elements.catalog.append(group);
  }
  if (!groups.size)
    elements.catalog.append(
      node(
        "p",
        "dialog-description",
        "No matching source formats. Try “PDF”, “Word”, or “images”.",
      ),
    );
}

elements.input.addEventListener("change", () => addFiles(elements.input.files));
elements.drop.addEventListener("click", () => elements.input.click());
elements.clear.addEventListener("click", () => {
  if (!busy) {
    queue.forEach(release);
    queue.length = 0;
    renderQueue();
    announce("Queue cleared.");
  }
});
elements.convert.addEventListener("click", () => runBatch());
$("#retry-connection").addEventListener("click", loadConfiguration);
for (const event of ["dragenter", "dragover", "dragleave", "drop"]) {
  window.addEventListener(event, (event) => {
    event.preventDefault();
  });
}
elements.drop.addEventListener("dragover", () => {
  if (!busy) elements.drop.classList.add("dragover");
});
elements.drop.addEventListener("dragleave", () =>
  elements.drop.classList.remove("dragover"),
);
elements.drop.addEventListener("drop", (event) => {
  elements.drop.classList.remove("dragover");
  addFiles(event.dataTransfer.files);
});
for (const selector of ["#formats-nav", "#formats-card", "#formats-more"]) {
  $(selector).addEventListener("click", () => {
    $("#formats-dialog").showModal();
  });
}
$("#privacy-button").addEventListener("click", () =>
  $("#privacy-dialog").showModal(),
);
$("#format-search").addEventListener("input", renderCatalog);
document.querySelectorAll("dialog").forEach((dialog) => {
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      dialog.close();
    }
  });
  dialog
    .querySelector(".close-dialog")
    .addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) {
      const rect = dialog.getBoundingClientRect();
      if (
        event.clientX < rect.left ||
        event.clientX > rect.right ||
        event.clientY < rect.top ||
        event.clientY > rect.bottom
      )
        dialog.close();
    }
  });
});
window.addEventListener("beforeunload", (event) => {
  if (busy) {
    event.preventDefault();
    event.returnValue = "";
  }
});
loadConfiguration();
