// Emit fighting68/layout.py from the catalog pulled out of the web bundle.
const fs = require('fs');
const c = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

const rows = c.fighting68;
const layout = rows.flat().map((k) => [k.value, k.name, k.key]);

// Widths in key units. The bundle only marks the wide caps by class name
// (.Backspace, .Tab, .Caps, .Enter, .ShiftMax, .Shift, .w55, .Spacebar-68,
// .key-line) and lets flexbox absorb the slack, so the units themselves are the
// standard ANSI 65% geometry the row structure matches exactly: 16 per row.
const WIDTH_BY_CLASS = {
  Backspace: 2,
  Tab: 1.5,
  'key-line': 1.5,
  Caps: 1.75,
  Enter: 2.25,
  ShiftMax: 2.25,
  Shift: 1.75,
  w55: 1.25,
  'Spacebar-68': 6.25,
};
const widthOf = (className) => {
  for (const token of String(className || '').split(/\s+/)) {
    if (token in WIDTH_BY_CLASS) return WIDTH_BY_CLASS[token];
  }
  return 1;
};

const usage = new Map(); // name -> hid usage
const reverse = new Map(); // usage -> name
for (const k of [...c.basic, ...c.extend]) {
  if (!usage.has(k.name)) usage.set(k.name, k.value);
  if (!reverse.has(k.value)) reverse.set(k.value, k.name);
}

const q = (s) => JSON.stringify(s);
let out = '# Generated from the vter.driveall.cn bundle by analysis/gen-layout.js.\n';
out += '# LAYOUT maps a key-map slot to its physical cap label; USAGE maps a label to a HID usage id.\n\n';
out += 'LAYOUT = {\n';
for (const [i, n] of layout) out += `    ${i}: ${q(n)},\n`;
out += '}\n\n';
out += '# Physical rows of (slot, width in key units), top to bottom.\n';
out += 'ROWS = [\n';
for (const row of rows) {
  out += '    [' + row.map((k) => `[${k.value}, ${widthOf(k.className)}]`).join(', ') + '],\n';
}
out += ']\n\n';
out += '# The usage id each cap sends by default, and the slot a rule for it lives in.\n';
out += 'SLOT_BY_USAGE = {\n';
const seen = new Set();
for (const [i, , key] of layout) {
  if (key && !seen.has(key)) {
    seen.add(key);
    out += `    ${key}: ${i},\n`;
  }
}
out += '}\n\nUSAGE = {\n';
for (const [n, v] of usage) out += `    ${q(n)}: ${v},\n`;
out += '}\n\nUSAGE_NAMES = {\n';
for (const [v, n] of reverse) out += `    ${v}: ${q(n)},\n`;
out += '}\n';
fs.writeFileSync(process.argv[3], out);

for (const row of rows) {
  const total = row.reduce((sum, k) => sum + widthOf(k.className), 0);
  if (total !== 16) throw new Error(`row sums to ${total} units, expected 16`);
}
console.log('rows', rows.length, 'keys', layout.length, 'bytes', out.length);
