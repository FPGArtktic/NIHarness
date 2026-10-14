// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! Rendering. Every widget reads state that was folded from events; nothing
//! here derives a number of its own.

use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Gauge, List, ListItem, Paragraph, Sparkline, Tabs, Wrap};
use ratatui::Frame;

use crate::app::{AppState, View};

/// Pipeline stages, in order, for the progress bar.
const STAGES: [&str; 8] = [
    "brainstorm",
    "spec_gate",
    "plan",
    "plan_gate",
    "implement",
    "commit_gate",
    "review",
    "full_gate",
];

/// Draw the whole frame.
pub(crate) fn draw(frame: &mut Frame, state: &AppState) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Min(5),
            Constraint::Length(1),
        ])
        .split(frame.area());

    draw_tabs(frame, chunks[0], state);
    match state.view {
        View::Work => draw_work(frame, chunks[1], state),
        View::Metrics => draw_metrics(frame, chunks[1], state),
        View::Bench => draw_placeholder(
            frame,
            chunks[1],
            "Bench",
            "No benchmark records yet. `nih bench` lands with the benchmark runner; \
             this view will read benchmarks/results/.",
        ),
        View::Log => draw_log(frame, chunks[1], state),
    }
    draw_status(frame, chunks[2], state);
}

fn draw_tabs(frame: &mut Frame, area: Rect, state: &AppState) {
    let titles: Vec<Line> = View::ALL
        .iter()
        .map(|view| Line::from(view.title()))
        .collect();
    let selected = View::ALL
        .iter()
        .position(|view| *view == state.view)
        .unwrap_or(0);
    let run = if state.run_id.is_empty() {
        "no run".to_string()
    } else {
        format!("run {}", state.run_id)
    };
    let tabs = Tabs::new(titles)
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title(format!(" NIHarness - {run} ")),
        )
        .select(selected)
        .highlight_style(
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        );
    frame.render_widget(tabs, area);
}

fn stage_line(state: &AppState) -> Line<'static> {
    let mut spans: Vec<Span> = Vec::new();
    for (index, stage) in STAGES.iter().enumerate() {
        let style = if *stage == state.stage {
            Style::default().fg(Color::Black).bg(Color::Cyan)
        } else {
            Style::default().fg(Color::DarkGray)
        };
        spans.push(Span::styled(format!(" {stage} "), style));
        if index + 1 < STAGES.len() {
            spans.push(Span::raw("→"));
        }
    }
    Line::from(spans)
}

fn draw_work(frame: &mut Frame, area: Rect, state: &AppState) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(3),
            Constraint::Length(3),
            Constraint::Min(4),
            Constraint::Length(7),
        ])
        .split(area);

    frame.render_widget(
        Paragraph::new(stage_line(state))
            .block(Block::default().borders(Borders::ALL).title(" Pipeline ")),
        rows[0],
    );

    let gate = match state.last_gate_passed {
        Some(true) => Span::styled("green", Style::default().fg(Color::Green)),
        Some(false) => Span::styled(
            "RED - no commit",
            Style::default().fg(Color::Red).add_modifier(Modifier::BOLD),
        ),
        None => Span::styled("not run", Style::default().fg(Color::DarkGray)),
    };
    let summary = Line::from(vec![
        Span::raw(format!(
            "role {}  ",
            if state.role.is_empty() {
                "-"
            } else {
                &state.role
            }
        )),
        Span::raw(format!("tools {}  ", state.tool_calls)),
        Span::raw(format!("rejected {}  ", state.rejected_calls)),
        Span::raw(format!("commits {}  ", state.commits.len())),
        Span::raw("gate "),
        gate,
    ]);
    frame.render_widget(
        Paragraph::new(summary).block(Block::default().borders(Borders::ALL).title(" Step ")),
        rows[1],
    );

    let items: Vec<ListItem> = state
        .activity
        .iter()
        .rev()
        .take(usize::from(rows[2].height).saturating_sub(2))
        .map(|entry| {
            ListItem::new(Line::from(vec![
                Span::styled(
                    format!("{:>5} ", entry.seq),
                    Style::default().fg(Color::DarkGray),
                ),
                Span::styled(
                    format!("{:20}", entry.kind),
                    Style::default().fg(Color::Cyan),
                ),
                Span::raw(entry.detail.clone()),
            ]))
        })
        .collect();
    frame.render_widget(
        List::new(items).block(Block::default().borders(Borders::ALL).title(" Activity ")),
        rows[2],
    );

    frame.render_widget(
        Paragraph::new(state.reasoning.clone())
            .wrap(Wrap { trim: true })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Reasoning (logged, never fed back) "),
            ),
        rows[3],
    );
}

fn draw_metrics(frame: &mut Frame, area: Rect, state: &AppState) {
    let rows = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(6),
            Constraint::Length(6),
            Constraint::Length(3),
            Constraint::Min(3),
        ])
        .split(area);

    frame.render_widget(
        Sparkline::default()
            .data(&state.generation_tps)
            .style(Style::default().fg(Color::Cyan))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Generation tok/s "),
            ),
        rows[0],
    );
    frame.render_widget(
        Sparkline::default()
            .data(&state.prompt_tps)
            .style(Style::default().fg(Color::Magenta))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Prompt processing tok/s "),
            ),
        rows[1],
    );

    let fill = state.context_fill.clamp(0.0, 1.0);
    let colour = if fill >= 0.75 {
        Color::Yellow
    } else {
        Color::Green
    };
    frame.render_widget(
        Gauge::default()
            .ratio(fill)
            .gauge_style(Style::default().fg(colour))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(" Context fill (compaction threshold 0.75) "),
            ),
        rows[2],
    );

    let totals = Paragraph::new(vec![
        Line::from(format!("tokens          {}", state.tokens)),
        Line::from(format!("compactions     {}", state.compactions)),
        Line::from(format!(
            "gate            {} runs, {} red",
            state.gate_runs, state.gate_failures
        )),
        Line::from(format!("max intervention {}", state.highest_intervention)),
    ])
    .block(Block::default().borders(Borders::ALL).title(" Totals "));
    frame.render_widget(totals, rows[3]);
}

fn draw_log(frame: &mut Frame, area: Rect, state: &AppState) {
    let items: Vec<ListItem> = state
        .activity
        .iter()
        .rev()
        .take(usize::from(area.height).saturating_sub(2))
        .map(|entry| {
            ListItem::new(format!(
                "{:>6} {} {} {}",
                entry.seq, entry.ts, entry.kind, entry.detail
            ))
        })
        .collect();
    frame.render_widget(
        List::new(items).block(
            Block::default()
                .borders(Borders::ALL)
                .title(" Event stream "),
        ),
        area,
    );
}

fn draw_placeholder(frame: &mut Frame, area: Rect, title: &str, text: &str) {
    frame.render_widget(
        Paragraph::new(text.to_string())
            .wrap(Wrap { trim: true })
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .title(format!(" {title} ")),
            ),
        area,
    );
}

fn draw_status(frame: &mut Frame, area: Rect, state: &AppState) {
    let text = format!(
        " Tab/F1-F4 switch view   q quit   events folded: {} ",
        state.seq
    );
    frame.render_widget(
        Paragraph::new(text).style(Style::default().fg(Color::Black).bg(Color::Cyan)),
        area,
    );
}
