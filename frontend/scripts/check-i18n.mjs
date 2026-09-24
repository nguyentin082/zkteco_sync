// Checks the two locales against each other and against the source:
//   - every key in one language exists in the other (plural forms aside:
//     English needs _one/_other, Vietnamese only _other);
//   - every literal t('…') / i18nKey="…" in src/ resolves in both.
// Run with `npm run check:i18n`; exits non-zero on any finding.
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, basename } from 'node:path'

const root = new URL('../src/', import.meta.url).pathname
const LANGS = ['en', 'vi']
const PLURAL = /_(zero|one|two|few|many|other)$/

function load(lang) {
  const dir = join(root, 'locales', lang)
  const out = {}
  for (const file of readdirSync(dir)) {
    out[basename(file, '.json')] = JSON.parse(readFileSync(join(dir, file), 'utf8'))
  }
  return out
}

function flatten(obj, prefix = '', out = new Set()) {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k
    if (v && typeof v === 'object') flatten(v, key, out)
    else out.add(key.replace(PLURAL, ''))
  }
  return out
}

function walk(dir, files = []) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, files)
    else if (/\.(jsx?|mjs)$/.test(name)) files.push(p)
  }
  return files
}

const keys = Object.fromEntries(LANGS.map((l) => [l, flatten(load(l))]))
const problems = []

for (const a of LANGS) {
  for (const b of LANGS) {
    if (a === b) continue
    for (const k of keys[a]) if (!keys[b].has(k)) problems.push(`${b}: missing ${k} (present in ${a})`)
  }
}

const USE = /(?:\bt\(|i18nKey=)\s*['"]([a-z_]+(?:\.[a-zA-Z0-9_]+)+)['"]/g
for (const file of walk(root)) {
  const src = readFileSync(file, 'utf8')
  for (const [, key] of src.matchAll(USE)) {
    for (const l of LANGS) {
      if (!keys[l].has(key)) problems.push(`${l}: ${file.replace(root, 'src/')} uses unknown key ${key}`)
    }
  }
}

if (problems.length) {
  console.error([...new Set(problems)].join('\n'))
  process.exit(1)
}
console.log(`i18n OK — ${keys.en.size} keys in ${LANGS.join(', ')}`)
