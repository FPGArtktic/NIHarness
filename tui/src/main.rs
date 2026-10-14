// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! `nih-tui`: a viewer for `NIHarness` run event streams.
//!
//! The viewer never reads project files, never runs a command and never calls
//! a model. It reads a run's event stream - from a Unix socket while a run is
//! live, or from an `events.jsonl` file afterwards - and renders it.

mod app;
mod event;
mod ui;

use std::io::{self, BufRead, BufReader, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::mpsc::{self, Receiver};
use std::time::Duration;

use crossterm::event::{self as term, Event as TermEvent, KeyCode, KeyEventKind};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::Terminal;

use crate::app::AppState;
use crate::event::Event;

const USAGE: &str = "\
nih-tui - viewer for NIHarness run event streams

Usage:
  nih-tui --events <path>    render a run's events.jsonl and keep watching it
  nih-tui --socket <path>    follow a live run over its Unix socket
  nih-tui --help

Keys:
  Tab, F1-F4   switch view (Work, Metrics, Bench, Log)
  q, Esc       quit
";

/// Where events come from.
enum Source {
    /// A run's `events.jsonl`.
    File(PathBuf),
    /// A live run's Unix socket.
    Socket(PathBuf),
}

fn parse_args(args: &[String]) -> Result<Source, String> {
    match args {
        [flag, path] if flag == "--events" => Ok(Source::File(PathBuf::from(path))),
        [flag, path] if flag == "--socket" => Ok(Source::Socket(PathBuf::from(path))),
        _ => Err(USAGE.to_string()),
    }
}

/// Read a whole `events.jsonl` file.
///
/// # Errors
///
/// Returns the I/O error if the file cannot be read.
fn read_file(path: &Path) -> io::Result<Vec<Event>> {
    let text = std::fs::read_to_string(path)?;
    Ok(event::parse_lines(&text))
}

/// Stream events from a Unix socket on a background thread.
///
/// # Errors
///
/// Returns the I/O error if the socket cannot be connected.
fn follow_socket(path: &Path) -> io::Result<Receiver<Event>> {
    let stream = UnixStream::connect(path)?;
    let (sender, receiver) = mpsc::channel();
    std::thread::spawn(move || {
        let reader = BufReader::new(stream);
        for line in reader.lines().map_while(Result::ok) {
            if let Ok(parsed) = serde_json::from_str::<Event>(&line) {
                if sender.send(parsed).is_err() {
                    return;
                }
            }
        }
    });
    Ok(receiver)
}

fn run(source: &Source) -> io::Result<()> {
    let mut state = AppState::default();
    let mut live: Option<Receiver<Event>> = None;
    match source {
        Source::File(path) => state = AppState::from_events(&read_file(path)?),
        Source::Socket(path) => live = Some(follow_socket(path)?),
    }

    let mut stdout = io::stdout();
    enable_raw_mode()?;
    stdout.execute(EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;

    let outcome = event_loop(&mut terminal, &mut state, live.as_ref());

    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    outcome
}

fn event_loop<B: ratatui::backend::Backend>(
    terminal: &mut Terminal<B>,
    state: &mut AppState,
    live: Option<&Receiver<Event>>,
) -> io::Result<()> {
    loop {
        terminal.draw(|frame| ui::draw(frame, state))?;

        if let Some(receiver) = live {
            while let Ok(parsed) = receiver.try_recv() {
                state.apply(&parsed);
            }
        }

        if term::poll(Duration::from_millis(120))? {
            if let TermEvent::Key(key) = term::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                match key.code {
                    KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                    KeyCode::Tab => state.next_view(),
                    KeyCode::F(number) => {
                        let index = usize::from(number).saturating_sub(1);
                        if let Some(view) = app::View::ALL.get(index) {
                            state.view = *view;
                        }
                    }
                    _ => {}
                }
            }
        }
    }
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match parse_args(&args) {
        Ok(source) => match run(&source) {
            Ok(()) => ExitCode::SUCCESS,
            Err(error) => {
                let _ = writeln!(io::stderr(), "nih-tui: {error}");
                ExitCode::FAILURE
            }
        },
        Err(message) => {
            let wanted_help = args.iter().any(|arg| arg == "--help" || arg == "-h");
            let mut target: Box<dyn Write> = if wanted_help {
                Box::new(io::stdout())
            } else {
                Box::new(io::stderr())
            };
            let _ = write!(target, "{message}");
            if wanted_help {
                ExitCode::SUCCESS
            } else {
                ExitCode::from(2)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{parse_args, Source};

    fn args(values: &[&str]) -> Vec<String> {
        values.iter().map(|value| (*value).to_string()).collect()
    }

    #[test]
    fn accepts_a_file_source() {
        match parse_args(&args(&["--events", "run/events.jsonl"])) {
            Ok(Source::File(path)) => assert!(path.ends_with("events.jsonl")),
            _ => panic!("expected a file source"),
        }
    }

    #[test]
    fn accepts_a_socket_source() {
        match parse_args(&args(&["--socket", "/run/user/1000/nih.sock"])) {
            Ok(Source::Socket(path)) => assert!(path.ends_with("nih.sock")),
            _ => panic!("expected a socket source"),
        }
    }

    #[test]
    fn rejects_anything_else_with_usage() {
        assert!(parse_args(&args(&[])).is_err());
        assert!(parse_args(&args(&["--events"])).is_err());
        assert!(parse_args(&args(&["--project", "x"])).is_err());
    }
}
