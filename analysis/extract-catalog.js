// One-off: pull the key catalog and Fighting68 physical layout out of the webpack bundle.
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// Each catalog entry is an object literal like { name: "A", value: 4 } or a richer
// layout entry { className: "...", value: 0, name: "Esc", ..., key: 41, keyCode: "Escape" }.
function extractArray(startMarker) {
  const at = src.indexOf(startMarker);
  if (at < 0) throw new Error('marker not found: ' + startMarker);
  const open = src.indexOf('[', at);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '[') depth++;
    else if (src[i] === ']') {
      depth--;
      if (depth === 0) return src.slice(open, i + 1);
    }
  }
  throw new Error('unbalanced: ' + startMarker);
}

function evalArray(text) {
  return eval('(' + text + ')');
}

const out = {};
for (const marker of ['key: "getFighting68"', 'key: "getAllKey"']) {
  out[marker] = 'see below';
}
out.basic = evalArray(extractArray('var Gi = function() {\n                return '));
out.extend = evalArray(extractArray('extendList: '));
out.special = evalArray(extractArray('specialList: '));
out.fighting68 = evalArray(extractArray('key: "getFighting68"'));
fs.writeFileSync(process.argv[3], JSON.stringify(out, null, 1));
for (const k of Object.keys(out)) {
  const v = out[k];
  console.log(k, Array.isArray(v) ? 'items=' + JSON.stringify(v).length : v);
}
console.log('rows in fighting68:', out.fighting68.length, 'keys:', out.fighting68.flat().length);
