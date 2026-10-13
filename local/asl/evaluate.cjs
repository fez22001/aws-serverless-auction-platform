// JSONata 2.0.6 evaluation only. $parse/$uuid are documented Step Functions extensions.
const readline = require('node:readline');
const { randomUUID } = require('node:crypto');
const jsonata = require('jsonata');
const cache = new Map();
const evaluate = async (value, bindings) => {
  if (typeof value === 'string' && value.startsWith('{% ') && value.endsWith(' %}')) {
    const source = value.slice(3, -3);
    if (!cache.has(source)) {
      const compiled = jsonata(source);
      compiled.registerFunction('parse', JSON.parse);
      compiled.registerFunction('uuid', randomUUID);
      cache.set(source, compiled);
    }
    const result = await cache.get(source).evaluate({}, bindings);
    if (result === undefined) throw new Error('JSONata evaluated to undefined: ' + source);
    return result;
  }
  if (Array.isArray(value)) return Promise.all(value.map(v => evaluate(v, bindings)));
  if (value && typeof value === 'object') {
    const entries = await Promise.all(Object.entries(value).map(async ([k,v]) => [k, await evaluate(v, bindings)]));
    return Object.fromEntries(entries);
  }
  return value;
};
(async () => {
  for await (const line of readline.createInterface({ input: process.stdin })) {
    try {
      const input = JSON.parse(line);
      process.stdout.write(JSON.stringify({ value: await evaluate(input.value, input.bindings) }) + '\n');
    } catch (error) { process.stdout.write(JSON.stringify({ error: String(error), stack: error.stack }) + '\n'); }
  }
})();
