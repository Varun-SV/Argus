//! `argus-next`: the preview build of the Argus command line (spec §12).
//!
//! Phase P0 provides only `--help` and `--version`. Commands are added in later phases with
//! exactly the names, options and exit codes listed in the parity inventory (spec §5 C-07);
//! none are invented here.

#![forbid(unsafe_code)]

use clap::Parser;

/// Argus preview (Rust re-architecture). Not yet a replacement for the `argus` command.
#[derive(Debug, Parser)]
#[command(
    name = argus_core::PREVIEW_BINARY_NAME,
    version = argus_core::PREVIEW_VERSION,
    about = "Argus preview build (Rust re-architecture, phase P0)",
    long_about = "Argus preview build (Rust re-architecture, phase P0).\n\n\
                  This binary exposes no commands yet. Keep using the `argus` command from the \
                  argus-app-testing package; see docs/rearchitecture/specification.md."
)]
struct Cli {}

fn main() {
    let _cli = Cli::parse();
    println!("{}", argus_core::preview_banner());
}

#[cfg(test)]
mod tests {
    use super::Cli;
    use clap::{CommandFactory, Parser};

    #[test]
    fn clap_definition_is_valid() {
        Cli::command().debug_assert();
    }

    #[test]
    fn no_subcommands_are_defined() {
        assert_eq!(Cli::command().get_subcommands().count(), 0);
    }

    #[test]
    fn unknown_argument_is_rejected() {
        let err = Cli::try_parse_from(["argus-next", "run"]).unwrap_err();
        assert_eq!(err.kind(), clap::error::ErrorKind::UnknownArgument);
    }
}
