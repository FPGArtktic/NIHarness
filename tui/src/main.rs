// SPDX-FileCopyrightText: 2026 Mateusz Okulanis
// SPDX-License-Identifier: GPL-3.0-or-later

//! `nih-tui`: a viewer for `NIHarness` run event streams.
//!
//! The viewer never reads project files, never runs a command and never calls
//! a model. It reads a run's event stream - from a Unix socket while a run is
//! live, or from an `events.jsonl` file afterwards - and renders it.

mod app;
mod control;
mod event;
mod ui;

use std::fs::File;
use std::io::{self, BufRead, BufReader, Read, Seek, SeekFrom, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

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
  nih-tui --run <dir>        follow .nih/runs/<id>/ live, and steer it
  nih-tui --events <path>    render one events.jsonl and stop
  nih-tui --socket <path>    follow a run over a Unix socket
  nih-tui --help

Keys:
  Tab, F1-F4   switch view (Work, Metrics, Bench, Log)
  c            ask the run to stop at its next step boundary
  q, Esc       quit
";

/// How often the viewer looks for new events in a followed file.
const POLL: Duration = Duration::from_millis(400);

/// Where events come from.
enum Source {
    /// A run's `events.jsonl`, read once.
    File(PathBuf),
    /// A run directory, followed while it runs and steerable.
    Run(PathBuf),
    /// A live run's Unix socket.
    Socket(PathBuf),
}

/// Reads the part of a file that has appeared since the last look.
struct Follower {
    path: PathBuf,
    offset: u64,
}

impl Follower {
    /// Start following `path` from the beginning.
    fn new(path: PathBuf) -> Self {
        Self { path, offset: 0 }
    }

    /// Return the events appended since the last call.
    ///
    /// Only whole lines are consumed: a run writing a line while the viewer
    /// reads must not have it parsed in halves.
    fn poll(&mut self) -> io::Result<Vec<Event>> {
        let mut file = match File::open(&self.path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(error),
        };
        file.seek(SeekFrom::Start(self.offset))?;
        let mut fresh = String::new();
        file.read_to_string(&mut fresh)?;
        let complete = match fresh.rfind('\n') {
            Some(index) => &fresh[..=index],
            None => return Ok(Vec::new()),
        };
        self.offset += complete.len() as u64;
        Ok(event::parse_lines(complete))
    }
}

fn parse_args(args: &[String]) -> Result<Source, String> {
    match args {
        [flag, path] if flag == "--events" => Ok(Source::File(PathBuf::from(path))),
        [flag, path] if flag == "--run" => Ok(Source::Run(PathBuf::from(path))),
        [flag, path] if flag == "--socket" => Ok(Source::Socket(PathBuf::from(path))),
        _ => Err(USAGE.to_string()),
    }
}

/// Return the run id a run directory belongs to.
fn run_id_of(run_dir: &Path) -> String {
    run_dir
        .file_name()
        .map(|name| name.to_string_lossy().into_owned())
        .unwrap_or_default()
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
    let mut follower: Option<Follower> = None;
    let mut run_dir: Option<PathBuf> = None;
    match source {
        Source::File(path) => state = AppState::from_events(&read_file(path)?),
        Source::Run(dir) => {
            follower = Some(Follower::new(dir.join("events.jsonl")));
            run_dir = Some(dir.clone());
        }
        Source::Socket(path) => live = Some(follow_socket(path)?),
    }

    let mut stdout = io::stdout();
    enable_raw_mode()?;
    stdout.execute(EnterAlternateScreen)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(stdout))?;

    let outcome = event_loop(
        &mut terminal,
        &mut state,
        live.as_ref(),
        follower.as_mut(),
        run_dir.as_deref(),
    );

    disable_raw_mode()?;
    io::stdout().execute(LeaveAlternateScreen)?;
    terminal.show_cursor()?;
    outcome
}

fn event_loop<B: ratatui::backend::Backend>(
    terminal: &mut Terminal<B>,
    state: &mut AppState,
    live: Option<&Receiver<Event>>,
    mut follower: Option<&mut Follower>,
    run_dir: Option<&Path>,
) -> io::Result<()> {
    // Checked, so the first iteration polls immediately even on a clock that
    // has not been up as long as the interval.
    let mut last_poll = Instant::now()
        .checked_sub(POLL)
        .unwrap_or_else(Instant::now);
    loop {
        terminal.draw(|frame| ui::draw(frame, state))?;

        if let Some(receiver) = live {
            while let Ok(parsed) = receiver.try_recv() {
                state.apply(&parsed);
            }
        }

        if let Some(tail) = follower.as_mut() {
            if last_poll.elapsed() >= POLL {
                last_poll = Instant::now();
                for parsed in tail.poll()? {
                    state.apply(&parsed);
                }
            }
        }

        if term::poll(Duration::from_millis(120))? {
            if let TermEvent::Key(key) = term::read()? {
                if key.kind != KeyEventKind::Press {
                    continue;
                }
                match key.code {
                    KeyCode::Char('q') | KeyCode::Esc => return Ok(()),
                    KeyCode::Char('c') => request_cancel(state, run_dir),
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

/// Ask the run to stop, and say what happened in the status bar.
fn request_cancel(state: &mut AppState, run_dir: Option<&Path>) {
    let Some(dir) = run_dir else {
        state.notice = "no run directory: start the viewer with --run".to_string();
        return;
    };
    let run_id = run_id_of(dir);
    state.notice = match control::submit_cancel(dir, &run_id, "cancelled from the TUI") {
        Ok(()) => "cancel requested; the run stops at its next step boundary".to_string(),
        Err(error) => format!("could not write the control inbox: {error}"),
    };
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
#[allow(
    clippy::expect_used,
    reason = "a test that cannot set its fixture up should fail loudly"
)]
mod tests {
    use super::{parse_args, run_id_of, Follower, Source};
    use std::fs;
    use std::io::Write;
    use std::path::PathBuf;

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
    fn accepts_a_run_directory() {
        match parse_args(&args(&[
            "--run",
            "/project/.nih/runs/20261022-100000-abcdef",
        ])) {
            Ok(Source::Run(path)) => assert!(path.ends_with("20261022-100000-abcdef")),
            _ => panic!("expected a run source"),
        }
    }

    #[test]
    fn a_run_directory_names_the_run() {
        assert_eq!(
            run_id_of(&PathBuf::from("/project/.nih/runs/20261022-100000-abcdef")),
            "20261022-100000-abcdef"
        );
    }

    #[test]
    fn following_a_log_returns_only_what_is_new() {
        let dir = std::env::temp_dir().join(format!("nih-follow-{}", std::process::id()));
        fs::create_dir_all(&dir).expect("the temporary directory is writable");
        let path = dir.join("events.jsonl");
        let line = |seq: u64| {
            format!(
                r#"{{"seq":{seq},"ts":"t","run_id":"r","body":{{"type":"tool.called","name":"grep"}}}}"#
            )
        };

        fs::write(&path, format!("{}\n", line(1))).expect("the log is writable");
        let mut follower = Follower::new(path.clone());
        assert_eq!(follower.poll().expect("first poll").len(), 1);
        assert_eq!(
            follower.poll().expect("second poll").len(),
            0,
            "nothing new"
        );

        let mut file = fs::OpenOptions::new()
            .append(true)
            .open(&path)
            .expect("the log is appendable");
        write!(file, "{}\n{}", line(2), line(3)).expect("two more lines, the last unfinished");
        drop(file);

        let fresh = follower.poll().expect("third poll");
        assert_eq!(fresh.len(), 1, "a half-written line waits for its newline");
        assert_eq!(fresh[0].seq, 2);
        fs::remove_dir_all(&dir).ok();
    }

    #[test]
    fn following_a_log_that_does_not_exist_yet_is_not_an_error() {
        let mut follower = Follower::new(PathBuf::from("/nonexistent/events.jsonl"));
        assert_eq!(follower.poll().expect("no file is not a failure").len(), 0);
    }

    #[test]
    fn rejects_anything_else_with_usage() {
        assert!(parse_args(&args(&[])).is_err());
        assert!(parse_args(&args(&["--events"])).is_err());
        assert!(parse_args(&args(&["--project", "x"])).is_err());
    }
}
