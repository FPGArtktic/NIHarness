// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! The event envelope, as `events.jsonl` stores it.
//!
//! The viewer is deliberately permissive about payload fields it does not know:
//! the harness may add an event type, and an older viewer must keep rendering
//! the ones it does understand instead of refusing the whole stream. The
//! authoritative definitions are the JSON Schemas under `schemas/`.

use serde::Deserialize;

/// One line of a run's event log.
#[derive(Debug, Clone, Deserialize)]
pub struct Event {
    /// Position in the run's stream, starting at one.
    pub seq: u64,
    /// When the sink accepted the payload, RFC 3339 in UTC.
    pub ts: String,
    /// The run this event belongs to.
    pub run_id: String,
    /// The step that produced it, when there was one.
    #[serde(default)]
    pub step_id: Option<String>,
    /// The payload.
    pub body: Body,
}

/// A payload, read field by field rather than as a closed enum.
#[derive(Debug, Clone, Deserialize)]
pub struct Body {
    /// The discriminator, for example `tool.called`.
    #[serde(rename = "type")]
    pub kind: String,
    /// Pipeline stage, on `step.started`.
    #[serde(default)]
    pub stage: Option<String>,
    /// Role, on `step.started`.
    #[serde(default)]
    pub role: Option<String>,
    /// Tool name, on `tool.called` and `tool.rejected`.
    #[serde(default)]
    pub name: Option<String>,
    /// Reasoning text, on `model.reasoning`.
    #[serde(default)]
    pub text: Option<String>,
    /// Result summary, on `tool.returned`.
    #[serde(default)]
    pub summary: Option<String>,
    /// Whether a gate tier passed, on `gate.finished`.
    #[serde(default)]
    pub passed: Option<bool>,
    /// Gate tier, on gate events.
    #[serde(default)]
    pub tier: Option<String>,
    /// Tokens the step consumed, on `step.finished`.
    #[serde(default)]
    pub tokens_used: Option<u64>,
    /// Commit sha, on `vcs.committed`.
    #[serde(default)]
    pub sha: Option<String>,
    /// Commit subject, on `vcs.committed`.
    #[serde(default)]
    pub subject: Option<String>,
    /// Generation rate, on `metrics.sampled`.
    #[serde(default)]
    pub generation_tps: Option<f64>,
    /// Prompt-processing rate, on `metrics.sampled`.
    #[serde(default)]
    pub prompt_tps: Option<f64>,
    /// Window fill as a fraction, on `metrics.sampled`.
    #[serde(default)]
    pub context_fill: Option<f64>,
    /// Intervention rung, on `loop.intervention`.
    #[serde(default)]
    pub level: Option<u8>,
    /// Pathology name, on loop events.
    #[serde(default)]
    pub pathology: Option<String>,
    /// Token count before compaction, on `context.compacted`.
    #[serde(default)]
    pub before_tokens: Option<u64>,
    /// Token count after compaction, on `context.compacted`.
    #[serde(default)]
    pub after_tokens: Option<u64>,
}

/// Parse a JSONL stream, skipping lines that are not events.
///
/// A malformed line is dropped rather than fatal: the viewer's job is to show
/// what a run did, not to validate the log. `nih status` is the strict reader.
#[must_use]
pub fn parse_lines(text: &str) -> Vec<Event> {
    text.lines()
        .filter(|line| !line.trim().is_empty())
        .filter_map(|line| serde_json::from_str::<Event>(line).ok())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::parse_lines;

    const TOOL_CALL: &str = r#"{"seq":1,"ts":"2026-10-14T18:22:41.511Z","run_id":"8f21c0",
        "step_id":"step-3","body":{"type":"tool.called","call_id":"c1","name":"read_file",
        "arguments":{"path":"src/net.c"},"arguments_digest":"3f9a4c1e2b7d8a05"}}"#;

    #[test]
    fn reads_a_known_payload() {
        let events = parse_lines(&TOOL_CALL.replace('\n', " "));
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].seq, 1);
        assert_eq!(events[0].body.kind, "tool.called");
        assert_eq!(events[0].body.name.as_deref(), Some("read_file"));
        assert_eq!(events[0].step_id.as_deref(), Some("step-3"));
    }

    #[test]
    fn keeps_an_unknown_payload_type() {
        let line = r#"{"seq":2,"ts":"t","run_id":"r","body":{"type":"future.event","x":1}}"#;
        let events = parse_lines(line);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].body.kind, "future.event");
    }

    #[test]
    fn skips_a_broken_line_without_losing_the_rest() {
        let text = format!("not json\n{}\n", TOOL_CALL.replace('\n', " "));
        assert_eq!(parse_lines(&text).len(), 1);
    }
}
