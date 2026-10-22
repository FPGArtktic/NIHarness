// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! Viewer state, folded from the event stream.
//!
//! The TUI computes nothing of its own: every number here is read from an
//! event. Metrics, context fill and compaction decisions are produced by the
//! harness, which is what keeps the two from disagreeing.

use crate::event::Event;

/// How many samples each metric series keeps.
pub(crate) const RING: usize = 300;

/// The views, switched with the function keys.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub(crate) enum View {
    /// Pipeline, active role, reasoning, tool calls, gate status.
    #[default]
    Work,
    /// Throughput, context fill, tokens per step.
    Metrics,
    /// Benchmark records.
    Bench,
    /// The raw event stream.
    Log,
}

impl View {
    /// The view's title, as shown in the tab bar.
    pub(crate) const fn title(self) -> &'static str {
        match self {
            Self::Work => "Work",
            Self::Metrics => "Metrics",
            Self::Bench => "Bench",
            Self::Log => "Log",
        }
    }

    /// Every view, in tab order.
    pub(crate) const ALL: [Self; 4] = [Self::Work, Self::Metrics, Self::Bench, Self::Log];
}

/// One line in the activity list.
#[derive(Debug, Clone)]
pub(crate) struct Activity {
    /// Event sequence number.
    pub(crate) seq: u64,
    /// When the sink accepted the event, as stored.
    pub(crate) ts: String,
    /// Event type.
    pub(crate) kind: String,
    /// A short human-readable description.
    pub(crate) detail: String,
}

/// Everything the viewer knows, derived only from events.
#[derive(Debug, Default)]
pub(crate) struct AppState {
    /// The run being watched.
    pub(crate) run_id: String,
    /// Sequence of the last event folded.
    pub(crate) seq: u64,
    /// Current pipeline stage.
    pub(crate) stage: String,
    /// Role of the active step.
    pub(crate) role: String,
    /// Identifier of the active step, when one is running.
    pub(crate) step_id: Option<String>,
    /// Tokens consumed by finished steps.
    pub(crate) tokens: u64,
    /// Executed tool calls.
    pub(crate) tool_calls: u64,
    /// Calls refused before execution.
    pub(crate) rejected_calls: u64,
    /// Gate runs, and how many were red.
    pub(crate) gate_runs: u64,
    /// Red gate runs.
    pub(crate) gate_failures: u64,
    /// Whether the last gate run passed.
    pub(crate) last_gate_passed: Option<bool>,
    /// Commits that landed, newest last.
    pub(crate) commits: Vec<String>,
    /// Compactions so far.
    pub(crate) compactions: u64,
    /// Highest intervention rung reached.
    pub(crate) highest_intervention: u8,
    /// Window fill, as a fraction.
    pub(crate) context_fill: f64,
    /// Generation rate samples.
    pub(crate) generation_tps: Vec<u64>,
    /// Prompt-processing rate samples.
    pub(crate) prompt_tps: Vec<u64>,
    /// The latest reasoning text, logged and never fed back to the model.
    pub(crate) reasoning: String,
    /// Recent activity, newest last.
    pub(crate) activity: Vec<Activity>,
    /// The selected view.
    pub(crate) view: View,
    /// A short line of feedback about something the viewer just did.
    pub(crate) notice: String,
}

#[allow(
    clippy::cast_possible_truncation,
    clippy::cast_sign_loss,
    reason = "token rates are small, finite and positive; the value is clamped first"
)]
fn push_sample(series: &mut Vec<u64>, value: f64) {
    let rounded = if value.is_finite() && value > 0.0 {
        value.round().min(f64::from(u32::MAX)) as u64
    } else {
        0
    };
    series.push(rounded);
    if series.len() > RING {
        series.remove(0);
    }
}

impl AppState {
    /// Fold one event into the state.
    pub(crate) fn apply(&mut self, event: &Event) {
        self.seq = event.seq;
        if self.run_id.is_empty() {
            event.run_id.clone_into(&mut self.run_id);
        }
        let body = &event.body;
        let mut detail = String::new();
        match body.kind.as_str() {
            "step.started" => {
                self.step_id.clone_from(&event.step_id);
                body.stage
                    .as_deref()
                    .unwrap_or_default()
                    .clone_into(&mut self.stage);
                body.role
                    .as_deref()
                    .unwrap_or_default()
                    .clone_into(&mut self.role);
                detail = format!("{} as {}", self.stage, self.role);
            }
            "step.finished" => {
                self.step_id = None;
                self.tokens += body.tokens_used.unwrap_or(0);
                detail = format!("{} tokens", body.tokens_used.unwrap_or(0));
            }
            "tool.called" => {
                self.tool_calls += 1;
                detail = body.name.clone().unwrap_or_default();
            }
            "tool.returned" => detail = body.summary.clone().unwrap_or_default(),
            "tool.rejected" => {
                self.rejected_calls += 1;
                detail = body.name.clone().unwrap_or_default();
            }
            "gate.finished" => {
                self.gate_runs += 1;
                let passed = body.passed.unwrap_or(false);
                if !passed {
                    self.gate_failures += 1;
                }
                self.last_gate_passed = Some(passed);
                detail = format!(
                    "{} {}",
                    body.tier.clone().unwrap_or_default(),
                    if passed { "green" } else { "RED" }
                );
            }
            "vcs.committed" => {
                if let Some(sha) = &body.sha {
                    self.commits.push(sha.clone());
                }
                detail = body.subject.clone().unwrap_or_default();
            }
            "context.compacted" => {
                self.compactions += 1;
                detail = format!(
                    "{} -> {} tokens",
                    body.before_tokens.unwrap_or(0),
                    body.after_tokens.unwrap_or(0)
                );
            }
            "loop.intervention" => {
                let level = body.level.unwrap_or(0);
                self.highest_intervention = self.highest_intervention.max(level);
                detail = format!(
                    "level {level} ({})",
                    body.pathology.clone().unwrap_or_default()
                );
            }
            "model.reasoning" => {
                body.text
                    .as_deref()
                    .unwrap_or_default()
                    .clone_into(&mut self.reasoning);
            }
            "metrics.sampled" => {
                if let Some(fill) = body.context_fill {
                    self.context_fill = fill;
                }
                push_sample(&mut self.generation_tps, body.generation_tps.unwrap_or(0.0));
                push_sample(&mut self.prompt_tps, body.prompt_tps.unwrap_or(0.0));
            }
            _ => {}
        }
        if body.kind != "metrics.sampled" {
            self.activity.push(Activity {
                seq: event.seq,
                ts: event.ts.clone(),
                kind: body.kind.clone(),
                detail,
            });
            if self.activity.len() > RING {
                self.activity.remove(0);
            }
        }
    }

    /// Fold a whole stream.
    pub(crate) fn from_events(events: &[Event]) -> Self {
        let mut state = Self::default();
        for event in events {
            state.apply(event);
        }
        state
    }

    /// Select the next view, wrapping around.
    pub(crate) fn next_view(&mut self) {
        let position = View::ALL.iter().position(|view| *view == self.view);
        let index = position.unwrap_or(0);
        self.view = View::ALL[(index + 1) % View::ALL.len()];
    }
}

#[cfg(test)]
mod tests {
    use super::{AppState, View};
    use crate::event::parse_lines;

    fn state_from(lines: &[&str]) -> AppState {
        AppState::from_events(&parse_lines(&lines.join("\n")))
    }

    #[test]
    fn folds_a_run_the_way_nih_status_does() {
        let state = state_from(&[
            r#"{"seq":1,"ts":"t","run_id":"8f21c0","body":{"type":"step.started","stage":"implement","role":"implementer"}}"#,
            r#"{"seq":2,"ts":"t","run_id":"8f21c0","body":{"type":"tool.called","name":"read_file"}}"#,
            r#"{"seq":3,"ts":"t","run_id":"8f21c0","body":{"type":"tool.rejected","name":"shell"}}"#,
            r#"{"seq":4,"ts":"t","run_id":"8f21c0","body":{"type":"gate.finished","tier":"quick","passed":false}}"#,
            r#"{"seq":5,"ts":"t","run_id":"8f21c0","body":{"type":"gate.finished","tier":"quick","passed":true}}"#,
            r#"{"seq":6,"ts":"t","run_id":"8f21c0","body":{"type":"vcs.committed","sha":"cafe","subject":"feat: x"}}"#,
            r#"{"seq":7,"ts":"t","run_id":"8f21c0","body":{"type":"step.finished","tokens_used":1234}}"#,
        ]);
        assert_eq!(state.run_id, "8f21c0");
        assert_eq!(state.seq, 7);
        assert_eq!(state.stage, "implement");
        assert_eq!(state.role, "implementer");
        assert_eq!(state.step_id, None, "a finished step leaves no active step");
        assert_eq!(state.tool_calls, 1);
        assert_eq!(state.rejected_calls, 1);
        assert_eq!(state.gate_runs, 2);
        assert_eq!(state.gate_failures, 1);
        assert_eq!(state.last_gate_passed, Some(true));
        assert_eq!(state.commits, vec!["cafe".to_string()]);
        assert_eq!(state.tokens, 1234);
    }

    #[test]
    fn metrics_samples_fill_the_series_but_not_the_activity_list() {
        let state = state_from(&[
            r#"{"seq":1,"ts":"t","run_id":"r","body":{"type":"metrics.sampled","generation_tps":12.4,"prompt_tps":301.6,"context_fill":0.62}}"#,
        ]);
        assert_eq!(state.generation_tps, vec![12]);
        assert_eq!(state.prompt_tps, vec![302]);
        assert!((state.context_fill - 0.62).abs() < f64::EPSILON);
        assert!(state.activity.is_empty());
    }

    #[test]
    fn interventions_keep_the_highest_rung() {
        let state = state_from(&[
            r#"{"seq":1,"ts":"t","run_id":"r","body":{"type":"loop.intervention","level":2,"pathology":"exact_repeat"}}"#,
            r#"{"seq":2,"ts":"t","run_id":"r","body":{"type":"loop.intervention","level":1,"pathology":"gate_thrash"}}"#,
        ]);
        assert_eq!(state.highest_intervention, 2);
    }

    #[test]
    fn reasoning_is_shown_but_kept_out_of_the_activity_detail() {
        let state = state_from(&[
            r#"{"seq":1,"ts":"t","run_id":"r","body":{"type":"model.reasoning","text":"thinking"}}"#,
        ]);
        assert_eq!(state.reasoning, "thinking");
        assert_eq!(state.activity.len(), 1);
        assert_eq!(state.activity[0].detail, "");
        assert_eq!(state.activity[0].ts, "t");
    }

    #[test]
    fn views_cycle() {
        let mut state = AppState::default();
        assert_eq!(state.view, View::Work);
        state.next_view();
        assert_eq!(state.view, View::Metrics);
        state.next_view();
        state.next_view();
        state.next_view();
        assert_eq!(state.view, View::Work);
    }

    #[test]
    fn an_unknown_event_type_does_not_disturb_the_counters() {
        let state = state_from(&[
            r#"{"seq":1,"ts":"t","run_id":"r","body":{"type":"future.event"}}"#,
            r#"{"seq":2,"ts":"t","run_id":"r","body":{"type":"tool.called","name":"grep"}}"#,
        ]);
        assert_eq!(state.tool_calls, 1);
        assert_eq!(state.activity.len(), 2);
    }
}
