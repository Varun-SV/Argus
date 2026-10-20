//! `argus-conformance`: the black-box conformance runner (docs/rearchitecture/specification.md
//! §13, C-07; parity inventory §1).
//!
//! It drives the `argus` command line of the Python implementation (`<python> -m argus`), the
//! Rust preview binary (`argus-next`) or any explicit command through its public interface
//! only, using data-driven case files (schema `argus-conformance-case-v1`, documented in
//! `tests/conformance/README.md`). Every case runs in a fresh sandbox with a private home and
//! XDG tree, no network and a hard timeout that kills the whole process group.

#![forbid(unsafe_code)]

pub mod assertions;
pub mod case;
pub mod jsonpath;
pub mod normalize;
pub mod process;
pub mod report;
pub mod runner;
pub mod sandbox;
pub mod shlex;
