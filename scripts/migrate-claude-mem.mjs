import { DatabaseSync } from 'node:sqlite';
import { promises as fs } from 'fs';
import path from 'path';
import { randomUUID } from 'node:crypto';

const CLAUDE_MEM_DB = path.join(process.env.USERPROFILE, '.claude-mem', 'claude-mem.db');
const OUTPUT_PATH = 'D:\\Claude\\memory.jsonl';

function sanitizeName(str) {
  return (str || 'unnamed')
    .replace(/[^a-zA-Z0-9_\-一-鿿]/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_|_$/g, '')
    .substring(0, 80);
}

function chunkText(text, maxLen = 250) {
  if (!text) return [];
  text = text.trim();
  if (text.length <= maxLen) return [text];
  const chunks = [];
  let remaining = text;
  while (remaining.length > 0) {
    if (remaining.length <= maxLen) {
      chunks.push(remaining);
      break;
    }
    const cut = remaining.lastIndexOf('.', maxLen);
    const idx = cut > maxLen / 2 ? cut : remaining.lastIndexOf(' ', maxLen);
    const split = idx > maxLen / 2 ? idx : maxLen;
    chunks.push(remaining.substring(0, split).trim());
    remaining = remaining.substring(split).trim();
  }
  return chunks.filter(Boolean);
}

function safeJsonParse(val) {
  if (!val) return [];
  if (Array.isArray(val)) return val;
  try {
    const parsed = JSON.parse(val);
    return Array.isArray(parsed) ? parsed : [val];
  } catch {
    return [val];
  }
}

async function main() {
  console.error('Opening claude-mem database...');
  const db = new DatabaseSync(CLAUDE_MEM_DB, { readonly: true, strict: true });

  // --- Read all source data ---
  const observations = db.prepare(`
    SELECT id, memory_session_id, project, type, title, subtitle, facts, narrative, concepts, text,
           created_at, files_read, files_modified, metadata, content_hash
    FROM observations ORDER BY id
  `).all();
  console.error(`Read ${observations.length} observations`);

  const sessionSummaries = db.prepare(`
    SELECT id, memory_session_id, project, request, investigated, learned, completed, next_steps,
           created_at, notes, files_read, files_edited
    FROM session_summaries ORDER BY id
  `).all();
  console.error(`Read ${sessionSummaries.length} session summaries`);

  const sessions = db.prepare(`
    SELECT id, memory_session_id, project, custom_title, started_at, status, user_prompt
    FROM sdk_sessions ORDER BY id
  `).all();
  console.error(`Read ${sessions.length} sessions`);

  // Memory session ID lookup
  const memorySessionToSdk = {};
  sessions.forEach(s => {
    if (s.memory_session_id) {
      memorySessionToSdk[s.memory_session_id] = s;
    }
  });

  db.close();

  // --- Build knowledge graph ---
  const entities = [];
  const relations = [];
  const entityNames = new Set();

  function addEntity(name, entityType, observationList) {
    const cleanObs = observationList
      .filter(o => o && o.trim())
      .map(o => o.trim().substring(0, 500));
    if (cleanObs.length === 0) return;
    if (entityNames.has(name)) {
      // Append to existing entity
      const existing = entities.find(e => e.name === name);
      if (existing) {
        const newObs = cleanObs.filter(o => !existing.observations.includes(o));
        existing.observations.push(...newObs);
      }
      return;
    }
    entityNames.add(name);
    entities.push({ name, entityType, observations: cleanObs });
  }

  function addRelation(from, to, relationType) {
    if (!from || !to) return;
    if (from === to) return;
    const dup = relations.some(r => r.from === from && r.to === to && r.relationType === relationType);
    if (!dup) {
      relations.push({ from, to, relationType });
    }
  }

  // 1. Project entities
  const projects = [...new Set(observations.map(o => o.project).filter(Boolean))];
  projects.forEach(p => {
    addEntity(`Project_${p}`, 'project', [`Project workspace: ${p}`]);
  });

  // 2. Session entities
  sessions.forEach(s => {
    const title = s.custom_title || s.started_at || 'unknown';
    addEntity(`Session_${s.memory_session_id || s.id}`, 'session', [
      `Started: ${s.started_at || 'unknown'}`,
      `Status: ${s.status || 'unknown'}`,
      s.user_prompt ? `Initial prompt: ${s.user_prompt.substring(0, 300)}` : null,
      s.custom_title ? `Title: ${s.custom_title}` : null,
    ].filter(Boolean));
  });

  // 3. Observation entities
  observations.forEach(obs => {
    const obsName = `Obs_${obs.id}_${sanitizeName(obs.title || 'untitled')}`;
    const obsLines = [];

    if (obs.title) obsLines.push(`📌 ${obs.title}`);
    if (obs.subtitle) obsLines.push(`📝 ${obs.subtitle}`);

    const facts = safeJsonParse(obs.facts);
    facts.forEach(f => obsLines.push(`🔹 Fact: ${f}`));

    if (obs.narrative) {
      obsLines.push(`📖 Narrative: ${obs.narrative.substring(0, 500)}`);
    }

    const concepts = safeJsonParse(obs.concepts);
    if (concepts.length > 0) {
      obsLines.push(`🏷️ Concepts: ${concepts.join(', ')}`);
    }

    if (obs.text) obsLines.push(`💬 ${obs.text.substring(0, 300)}`);
    obsLines.push(`🕐 Created: ${obs.created_at || 'unknown'}`);
    if (obs.files_read) obsLines.push(`📂 Files read: ${obs.files_read.substring(0, 300)}`);
    if (obs.files_modified) obsLines.push(`✏️ Files modified: ${obs.files_modified.substring(0, 300)}`);

    addEntity(obsName, obs.type || 'observation', obsLines);

    // Relations
    if (obs.project) {
      addRelation(obsName, `Project_${obs.project}`, 'belongs_to_project');
    }
    if (obs.memory_session_id) {
      const sdk = memorySessionToSdk[obs.memory_session_id];
      const sessionName = `Session_${obs.memory_session_id}`;
      if (entityNames.has(sessionName)) {
        addRelation(obsName, sessionName, 'observed_in_session');
      }
      if (sdk && obs.project && sdk.project && obs.project !== sdk.project) {
        addRelation(sessionName, `Project_${obs.project}`, 'session_in_project');
      }
    }
  });

  // 4. Session summary entities
  sessionSummaries.forEach(s => {
    const summaryName = `Summary_${s.id}_session_${(s.memory_session_id || 'unknown').substring(0, 8)}`;
    const lines = [];
    if (s.request) lines.push(`❓ Request: ${s.request.substring(0, 400)}`);
    if (s.investigated) lines.push(`🔍 Investigated: ${s.investigated.substring(0, 400)}`);
    if (s.learned) lines.push(`💡 Learned: ${s.learned.substring(0, 400)}`);
    if (s.completed) lines.push(`✅ Completed: ${s.completed.substring(0, 400)}`);
    if (s.next_steps) lines.push(`⏭️ Next steps: ${s.next_steps.substring(0, 300)}`);
    if (s.notes) lines.push(`📝 Notes: ${s.notes.substring(0, 300)}`);
    if (s.files_read) lines.push(`📂 Files read: ${s.files_read.substring(0, 200)}`);
    if (s.files_edited) lines.push(`✏️ Files edited: ${s.files_edited.substring(0, 200)}`);
    lines.push(`🕐 Created: ${s.created_at || 'unknown'}`);

    addEntity(summaryName, 'session_summary', lines);

    if (s.project) {
      addRelation(summaryName, `Project_${s.project}`, 'belongs_to_project');
    }
    if (s.memory_session_id && entityNames.has(`Session_${s.memory_session_id}`)) {
      addRelation(summaryName, `Session_${s.memory_session_id}`, 'summarizes_session');
    }
  });

  // --- Build JSONL ---
  const lines = [
    ...entities.map(e => JSON.stringify({ type: 'entity', ...e })),
    ...relations.map(r => JSON.stringify({ type: 'relation', ...r })),
  ];

  await fs.writeFile(OUTPUT_PATH, lines.join('\n'), 'utf-8');

  // --- Summary ---
  console.error('');
  console.error('=== MIGRATION COMPLETE ===');
  console.error(`Output: ${OUTPUT_PATH}`);
  console.error(`Entities: ${entities.length}`);
  console.error(`Relations: ${relations.length}`);
  console.error(`Total lines: ${lines.length}`);
  console.error('');
  console.error('Entity types breakdown:');
  const typeCount = {};
  entities.forEach(e => { typeCount[e.entityType] = (typeCount[e.entityType] || 0) + 1; });
  Object.entries(typeCount).sort((a, b) => b[1] - a[1]).forEach(([t, c]) => {
    console.error(`  ${t}: ${c}`);
  });
  console.error('');
  console.error('Relation types breakdown:');
  const relCount = {};
  relations.forEach(r => { relCount[r.relationType] = (relCount[r.relationType] || 0) + 1; });
  Object.entries(relCount).sort((a, b) => b[1] - a[1]).forEach(([t, c]) => {
    console.error(`  ${t}: ${c}`);
  });
}

main().catch(err => {
  console.error('Migration failed:', err);
  process.exit(1);
});
