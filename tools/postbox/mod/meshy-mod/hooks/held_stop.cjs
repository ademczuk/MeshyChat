#!/usr/bin/env node
// meshy-mod's held-mail guard as a COMMAND Stop hook (design 12651e, pxx GO-on-design 9dff03, a reviewer 567d6d).
//
// Why a command hook: on a seat with managed settings, the security built-in forwards classic.* events past
// user-tier mods, so meshy.js's classic.Stop guard never runs there ("meshy-mod: its classic.Stop hook was
// skipped"). Declarative command Stop hooks still run on those seats (stop_hook_summary on HQD and pxx's Mac).
//
// What it does: a stop is blocked when this seat's postbox bank holds cc mail without a wake that nothing has
// named yet: no notice (<log>.held-named.json), no read (<state>.read.json, the mod's read_ledger), no earlier
// block of this session. It blocks again only when the held set gains a key (pxx 3), at most once in
// STOP_BLOCK_MS, and never while stop_hook_active is set. The reason carries keys, sender labels and ages only:
// a block's reason reaches the model, and peer text there would steer a seat outside the seat_directive path.
//
// It is local and read-only toward the mesh: no client or server call (pxx 1). It fails OPEN: anything it cannot
// read or judge means no block, and it always exits 0. Its only write is the per-session state file, 0600,
// atomic, bounded, keyed by session_id (pxx 2); a bad state file means this session is not judged.
'use strict'

const fs = require('fs')
const os = require('os')
const path = require('path')
const { spawnSync } = require('child_process')

const STOP_BLOCK_MS = 15 * 60_000
const HELD_PEEK_MS = 5_000
const HELD_STOP_SHOWN = 5
const STATE_KEYS_MAX = 500
const READ_MAX_BYTES = 2_000_000
const HELD_KEY = /^msg\/[A-Za-z0-9][A-Za-z0-9._:-]{0,80}$/
const SESSION_ID = /^[A-Za-z0-9-]{1,80}$/

function readBoundedJson(p, limit = READ_MAX_BYTES) {
  const st = fs.lstatSync(p) // no-follow: a link is refused, not read
  if (!st.isFile()) throw new Error('not a plain file')
  if (st.size > limit) throw new Error('over ' + limit + ' bytes')
  return JSON.parse(fs.readFileSync(p, 'utf8'))
}

function exists(p) {
  try {
    fs.lstatSync(p)
    return true
  } catch {
    return false
  }
}

// A read ledger: a bare JSON list of keys, or the client's {"schema_version": 1, "reads": [{"key"}]}.
function ledgerKeys(doc) {
  if (Array.isArray(doc)) return doc.filter((k) => typeof k === 'string')
  if (doc && typeof doc === 'object' && doc.schema_version === 1 && Array.isArray(doc.reads)) {
    return doc.reads.filter((r) => r && typeof r.key === 'string').map((r) => r.key)
  }
  return []
}

function homeDir(env) {
  return String(env.USERPROFILE || env.HOME || os.homedir() || '').replace(/\\/g, '/')
}

// The fallback waiters SEAT-SETUP documents for a session started without the channel: wait_next.py
// --consumer hook (and pxx's cc), and wait_next.py's own default, session. peek_bank has no fallback between
// consumers, so all are peeked and their held rows joined (pxx P2-3); a consumer with no bank holds none.
const FALLBACK_CONSUMERS = ['hook', 'cc', 'session']
const CONSUMER = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/

// The identities this session speaks for, and the bank consumers to peek (antigravity 3). The consumers are
// meshy-mod.json's `held_consumers` when it names any, else the documented fallback waiters. MESHY_IDENTITY adds
// the Claude channel's bank in front of them, and does not replace them: HQB's documented non-channel resume keeps
// MESHY_IDENTITY set and waits with `wait_next.py --consumer cc` (SEAT-SETUP, resume), so a channel-only peek
// missed that bank's held mail (GitHub Codex on #557 :72). Peeking a bank that holds nothing costs nothing, and a
// key this session read or was already shown never blocks again, whichever bank holds it.
function wantedIdents(env, modCfg) {
  const named = Array.isArray(modCfg.held_consumers) ? modCfg.held_consumers.filter((s) => typeof s === 'string' && CONSUMER.test(s)) : []
  const base = named.length ? named : FALLBACK_CONSUMERS
  const fromEnv = String(env.MESHY_IDENTITY || '').split(',').map((s) => s.trim()).filter(Boolean)
  if (fromEnv.length) return { names: fromEnv, consumers: ['claude-channel', ...base.filter((c) => c !== 'claude-channel')] }
  const fromCfg = Array.isArray(modCfg.identities) ? modCfg.identities.filter((s) => typeof s === 'string' && s) : []
  return { names: fromCfg, consumers: base }
}

function modLedgerPaths(modCfg, names) {
  const rl = modCfg.read_ledger
  const out = {}
  if (typeof rl === 'string' && rl && names.length) out[names[0]] = [rl]
  else if (rl && typeof rl === 'object' && !Array.isArray(rl)) {
    for (const [n, p] of Object.entries(rl)) if (typeof p === 'string' && p) out[n] = [p]
  }
  return out
}

// One identity's held rows, or { error }. `run` is spawnSync-shaped (a seam for tests).
function peekIdent(ident, configPath, consumers, run = spawnSync) {
  const named = new Set()
  const namedPath = ident.log + '.held-named.json'
  if (exists(namedPath)) {
    const doc = readBoundedJson(namedPath)
    const keys = doc && typeof doc === 'object' ? doc.keys : null
    if (!Array.isArray(keys) || !keys.every((k) => typeof k === 'string')) throw new Error('the named list is not a list of keys')
    for (const k of keys) named.add(k)
  }
  const helper = path.join(__dirname, 'held_peek.py')
  const r = run(ident.python || 'python', ['-I', '-B', helper, ident.client, configPath, ident.name, consumers.join(',')], {
    cwd: ident.client, timeout: HELD_PEEK_MS, encoding: 'utf8', windowsHide: true,
  })
  if (r.error) throw new Error(String(r.error.code || r.error.message || r.error))
  if (r.status !== 0) throw new Error('exit ' + r.status)
  const out = JSON.parse(String(r.stdout || '').trim().split('\n').pop())
  if (out.error) throw new Error(String(out.error).slice(0, 80))
  const read = new Set(Array.isArray(out.read) ? out.read.filter((k) => typeof k === 'string') : [])
  const rows = []
  for (const h of Array.isArray(out.held) ? out.held : []) {
    if (typeof h?.key !== 'string' || !HELD_KEY.test(h.key)) continue
    const label = String(h.from || '').replace(/[^A-Za-z0-9._-]/g, '').slice(0, 32) || 'unknown'
    rows.push({ key: h.key, label, heldAt: Date.parse(String(h.held_at || '')) })
  }
  return { rows, named, read }
}

function age(ms) {
  const m = Math.floor(ms / 60_000)
  return m < 1 ? '<1m' : m < 60 ? m + 'm' : Math.floor(m / 60) + 'h' + String(m % 60).padStart(2, '0') + 'm'
}

// Oldest first; a row with no held time sorts as now (newest).
function oldestFirst(rows, now) {
  const at = (h) => (Number.isFinite(h.heldAt) ? h.heldAt : now)
  return [...rows].sort((a, b) => at(a) - at(b))
}

// The rows a block shows: the oldest HELD_STOP_SHOWN. Only these count as named (pxx P2-1, as
// meshy_channel's record_held_shown names only the keys its held line shows).
function shownRows(rows, now) {
  return oldestFirst(rows, now).slice(0, HELD_STOP_SHOWN)
}

function heldText(rows, now) {
  const shown = shownRows(rows, now).map((h) => h.key + ' (' + h.label + ', ' + age(Number.isFinite(h.heldAt) ? Math.max(0, now - h.heldAt) : 0) + ')')
  const more = rows.length - HELD_STOP_SHOWN
  return rows.length + ' cc message' + (rows.length === 1 ? ' is' : 's are') + ' held for this seat without a wake: ' +
    shown.join(', ') + (more > 0 ? ' (+' + more + ')' : '') + '.'
}

// Pure: what to do with the fresh rows given this session's state. Returns { block: text|null, state }.
// Only the keys the block SHOWS are recorded, so a key hidden in "(+N)" blocks again after STOP_BLOCK_MS.
function decide(rows, state, now) {
  const seen = new Set(state.named)
  const fresh = rows.filter((h) => !seen.has(h.key))
  if (!fresh.length) return { block: null, state }
  if (now - state.blocked_at < STOP_BLOCK_MS) return { block: null, state }
  const named = [...state.named, ...shownRows(fresh, now).map((h) => h.key)].slice(-STATE_KEYS_MAX)
  return {
    block: 'meshy-mod: ' + heldText(fresh, now) + ' Read them, or say that none needs a reply.',
    state: { v: 1, named, blocked_at: now },
  }
}

function statePath(home, sessionId) {
  return path.join(home, '.meshychat', 'held-stop', sessionId + '.json')
}

// A missing file is a fresh session; anything else that is not our shape throws (the session is not judged).
function loadState(p) {
  if (!exists(p)) return { v: 1, named: [], blocked_at: -Infinity }
  const doc = readBoundedJson(p, 200_000)
  if (!doc || doc.v !== 1 || !Array.isArray(doc.named) || !doc.named.every((k) => typeof k === 'string') ||
      typeof doc.blocked_at !== 'number') throw new Error('the session state is not ours')
  return { v: 1, named: doc.named.slice(-STATE_KEYS_MAX), blocked_at: doc.blocked_at }
}

function saveState(p, state) {
  fs.mkdirSync(path.dirname(p), { recursive: true, mode: 0o700 })
  const tmp = p + '.' + process.pid + '.tmp'
  const fd = fs.openSync(tmp, 'wx', 0o600) // exclusive: never through someone else's file or link
  try {
    fs.writeSync(fd, JSON.stringify(state))
  } finally {
    fs.closeSync(fd)
  }
  fs.renameSync(tmp, p)
}

function main(input, env = process.env, run = spawnSync, now = Date.now()) {
  if (!input || input.stop_hook_active) return null // never twice in a row
  const sid = String(input.session_id || '')
  if (!SESSION_ID.test(sid)) return null
  const home = homeDir(env)
  let modCfg = {}
  try {
    modCfg = readBoundedJson(env.MESHY_MOD_CONFIG || home + '/.meshychat/meshy-mod.json')
    if (!modCfg || typeof modCfg !== 'object') modCfg = {}
  } catch {
    modCfg = {}
  }
  const { names, consumers } = wantedIdents(env, modCfg)
  if (!names.length) return null
  const configPath = env.MESHY_POSTBOX_CONFIG || home + '/.meshychat/postbox/config.json'
  const cfg = readBoundedJson(configPath, 1_000_000)
  const idents = (Array.isArray(cfg?.identities) ? cfg.identities : [])
    .filter((i) => i && names.includes(i.name) && typeof i.log === 'string' && typeof i.client === 'string' && i.enabled !== false)
  const ledgers = modLedgerPaths(modCfg, names)
  const rows = []
  for (const id of idents) {
    let got
    try {
      got = peekIdent(id, configPath, consumers, run)
      for (const p of ledgers[id.name] || []) {
        try {
          for (const k of ledgerKeys(readBoundedJson(p))) got.read.add(k)
        } catch {
          // an unreadable mod ledger changes nothing, as in meshy.js
        }
      }
    } catch {
      continue // not judged: nothing of this identity blocks
    }
    for (const h of got.rows) if (!got.named.has(h.key) && !got.read.has(h.key)) rows.push(h)
  }
  if (!rows.length) return null
  const sp = statePath(home, sid)
  const state = loadState(sp) // throws on a bad file: the caller fails open
  const d = decide(rows, state, now)
  if (!d.block) return null
  saveState(sp, d.state) // record before blocking: a failed write means no block, never a repeated one
  return { decision: 'block', reason: d.block }
}

if (require.main === module) {
  let raw = ''
  process.stdin.setEncoding('utf8')
  process.stdin.on('data', (c) => {
    raw += c
    if (raw.length > 1_000_000) raw = '' // not a hook payload; judge nothing
  })
  process.stdin.on('end', () => {
    try {
      const out = main(JSON.parse(raw || '{}'))
      if (out) process.stdout.write(JSON.stringify(out) + '\n')
    } catch {
      // fail open
    }
    process.exit(0)
  })
}

module.exports = { main, decide, heldText, loadState, saveState, peekIdent, wantedIdents, ledgerKeys, STOP_BLOCK_MS }
