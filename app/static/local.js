// Small, dependency-free converters. These functions never make network requests.
const MAX_TEXT = 1_000_000;
const MAX_CELLS = 200_000;

export function parseDelimited(text, delimiter = ",") {
  const rows = [];
  let row = [],
    field = "",
    quoted = false,
    closed = false;
  text = text.replace(/^\uFEFF/, "");
  if (!text) return [];
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (quoted) {
      if (char === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i++;
        } else {
          quoted = false;
          closed = true;
        }
      } else field += char;
    } else if (char === delimiter) {
      row.push(field);
      field = "";
      closed = false;
    } else if (char === "\r" || char === "\n") {
      if (char === "\r" && text[i + 1] === "\n") i++;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
      closed = false;
    } else if (char === '"' && !field && !closed) quoted = true;
    else {
      if (closed || char === '"')
        throw new Error(
          "Invalid CSV quoting. Check the file’s delimiters and quotation marks.",
        );
      field += char;
    }
  }
  if (quoted)
    throw new Error("A quoted cell is missing its closing quotation mark.");
  if (field || row.length || closed || !/[\r\n]$/.test(text)) {
    row.push(field);
    rows.push(row);
  }
  checkCells(rows);
  return rows;
}

function checkCells(rows) {
  if (rows.reduce((count, row) => count + row.length, 0) > MAX_CELLS) {
    throw new Error("This spreadsheet exceeds the 200,000 cell limit.");
  }
  return rows;
}

export function jsonToRows(text) {
  const data = JSON.parse(text);
  if (!Array.isArray(data))
    throw new Error("JSON must be an array of objects or rows.");
  if (!data.length) return [];
  let rows;
  if (
    data.every((row) => row && typeof row === "object" && !Array.isArray(row))
  ) {
    const headers = [...new Set(data.flatMap((row) => Object.keys(row)))];
    rows = [
      headers,
      ...data.map((row) => headers.map((key) => row[key] ?? "")),
    ];
  } else if (data.every(Array.isArray)) rows = data;
  else
    throw new Error(
      "JSON must contain only objects, or only arrays of cell values.",
    );
  return checkCells(
    rows.map((row) =>
      row.map((cell) =>
        cell === null
          ? ""
          : typeof cell === "object"
            ? JSON.stringify(cell)
            : cell,
      ),
    ),
  );
}

export function rowsToJson(rows) {
  if (!rows.length) return "[]";
  const headers = rows[0].map(String);
  if (
    headers.some((header) => !header) ||
    new Set(headers).size !== headers.length
  ) {
    throw new Error(
      "JSON export needs unique, non-empty column headers in the first row.",
    );
  }
  if (rows.slice(1).some((row) => row.length > headers.length)) {
    throw new Error("A row has more cells than there are column headers.");
  }
  return JSON.stringify(
    rows
      .slice(1)
      .map((row) =>
        Object.fromEntries(
          headers.map((header, index) => [header, row[index] ?? ""]),
        ),
      ),
    null,
    2,
  );
}

export function serializeDelimited(rows, delimiter = ",") {
  return (
    rows
      .map((row) =>
        row
          .map((cell) => {
            let text = String(cell ?? "");
            // Do not activate formulas when the exported file is opened in a spreadsheet.
            if (typeof cell === "string" && /^[=+\-@\t\r]/.test(text))
              text = "'" + text;
            return /["\r\n]/.test(text) || text.includes(delimiter)
              ? '"' + text.replaceAll('"', '""') + '"'
              : text;
          })
          .join(delimiter),
      )
      .join("\r\n") + (rows.length ? "\r\n" : "")
  );
}

export function escapeHtml(text) {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function readText(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const encoding =
    bytes[0] === 255 && bytes[1] === 254
      ? "utf-16le"
      : bytes[0] === 254 && bytes[1] === 255
        ? "utf-16be"
        : "utf-8";
  let text;
  try {
    text = new TextDecoder(encoding, { fatal: true }).decode(bytes);
  } catch {
    throw new Error("Use a UTF-8 or UTF-16 encoded text file.");
  }
  if (text.includes("\0"))
    throw new Error("This file contains binary data, not readable text.");
  if (text.length > MAX_TEXT)
    throw new Error(
      "Text content exceeds the 1 million character processing limit.",
    );
  return text;
}

export async function convertLocally(file, source, target) {
  if (["png", "jpg", "webp"].includes(source)) {
    let image;
    try {
      image = await createImageBitmap(file, { imageOrientation: "from-image" });
    } catch {
      throw new Error(
        "This image could not be read. Check that it is a valid image file.",
      );
    }
    try {
      if (image.width * image.height > 25_000_000)
        throw new Error(
          "This image exceeds the 25 megapixel processing limit.",
        );
      const canvas = document.createElement("canvas");
      canvas.width = image.width;
      canvas.height = image.height;
      const context = canvas.getContext("2d");
      if (!context)
        throw new Error("Your browser does not support image conversion.");
      if (target === "jpg") {
        context.fillStyle = "#ffffff";
        context.fillRect(0, 0, canvas.width, canvas.height);
      }
      context.drawImage(image, 0, 0);
      const type = "image/" + (target === "jpg" ? "jpeg" : target);
      const result = await new Promise((resolve) =>
        canvas.toBlob(resolve, type, 0.92),
      );
      canvas.width = 0;
      canvas.height = 0;
      if (!result || result.type !== type)
        throw new Error(
          "Your browser cannot encode that image format. Try PNG or JPEG.",
        );
      return result;
    } finally {
      image.close();
    }
  }
  const text = await readText(file);
  if (["csv", "tsv", "json"].includes(source)) {
    const rows =
      source === "json"
        ? jsonToRows(text)
        : parseDelimited(text, source === "tsv" ? "\t" : ",");
    return new Blob(
      [
        target === "json"
          ? rowsToJson(rows)
          : serializeDelimited(rows, target === "tsv" ? "\t" : ","),
      ],
      {
        type:
          target === "json"
            ? "application/json"
            : "text/" +
              (target === "tsv" ? "tab-separated-values" : "csv") +
              ";charset=utf-8",
      },
    );
  }
  if (source === "html" && target === "txt") {
    const doc = new DOMParser().parseFromString(text, "text/html");
    doc
      .querySelectorAll("script,style,noscript,template,head")
      .forEach((node) => node.remove());
    doc.querySelectorAll("br").forEach((node) => node.replaceWith("\n"));
    doc
      .querySelectorAll("p,div,section,article,li,h1,h2,h3,h4,h5,h6,tr")
      .forEach((node) => node.append("\n"));
    return new Blob([(doc.body.textContent || "").trim()], {
      type: "text/plain;charset=utf-8",
    });
  }
  if (target === "html") {
    return new Blob(
      [
        '<!doctype html><html><head><meta charset="utf-8"><title>Converted document</title></head><body><pre>' +
          escapeHtml(text) +
          "</pre></body></html>",
      ],
      { type: "text/html;charset=utf-8" },
    );
  }
  return new Blob([text], { type: "text/plain;charset=utf-8" });
}
