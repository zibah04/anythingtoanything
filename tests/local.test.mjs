import test from "node:test";
import assert from "node:assert/strict";
import {
  parseDelimited,
  serializeDelimited,
  jsonToRows,
  rowsToJson,
  escapeHtml,
} from "../app/static/local.js";

test("CSV handles quoted commas, escaped quotes, newlines, BOM, and empty cells", () => {
  assert.deepEqual(
    parseDelimited(
      '\uFEFFname,note,empty\r\nAlice,"A, B and ""C""",\r\nBob,"two\nlines",\r\n',
    ),
    [
      ["name", "note", "empty"],
      ["Alice", 'A, B and "C"', ""],
      ["Bob", "two\nlines", ""],
    ],
  );
  assert.deepEqual(parseDelimited("a,b\n1,2"), [
    ["a", "b"],
    ["1", "2"],
  ]);
  assert.deepEqual(parseDelimited(""), []);
  assert.deepEqual(parseDelimited('""'), [[""]]);
});
test("invalid CSV is rejected instead of silently losing content", () => {
  assert.throws(() => parseDelimited('a,b\n"open'), /closing/);
  assert.throws(() => parseDelimited('a,b\n"closed"garbage'), /quoting/);
});
test("CSV to JSON preserves strings, special property names, and missing cells", () => {
  assert.deepEqual(
    JSON.parse(
      rowsToJson([
        ["name", "__proto__", "n"],
        ["Alice", "safe"],
      ]),
    ),
    [{ name: "Alice", ["__proto__"]: "safe", n: "" }],
  );
  assert.throws(
    () =>
      rowsToJson([
        ["a", "a"],
        [1, 2],
      ]),
    /unique/,
  );
  assert.throws(() => rowsToJson([["a"], [1, 2]]), /more cells/);
});
test("JSON rows use the union of keys and encode nested values", () => {
  assert.deepEqual(
    jsonToRows('[{"name":"Alice","meta":{"age":2}},{"extra":true}]'),
    [
      ["name", "meta", "extra"],
      ["Alice", '{"age":2}', ""],
      ["", "", true],
    ],
  );
  assert.throws(() => jsonToRows('{"a":1}'), /array/);
  assert.throws(() => jsonToRows('[1, {"a":1}]'), /only/);
});
test("delimited exports escape cells and prevent spreadsheet formulas", () => {
  assert.equal(
    serializeDelimited([
      ["a", "b"],
      ["=1+1", "x,y"],
      ["line\n2", '"'],
    ]),
    'a,b\r\n\'=1+1,"x,y"\r\n"line\n2",""""\r\n',
  );
  assert.equal(serializeDelimited([[-1, "-1"]], "\t"), "-1\t'-1\r\n");
});
test("HTML escapes file content", () => {
  assert.equal(
    escapeHtml("<script>\"&'</script>"),
    "&lt;script&gt;&quot;&amp;&#39;&lt;/script&gt;",
  );
});
