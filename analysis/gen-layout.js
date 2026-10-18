// Emit fighting68/layout.py from the catalog pulled out of the web bundle.
const fs = require('fs');
const c = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

const layout = c.fighting68.flat().map((k) => [k.value, k.name, k.key]);

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
console.log('layout entries', layout.length, 'usage names', usage.size, 'bytes', out.length);
