//! Build script for the `_native` extension module.
//!
//! On macOS a Python extension must leave the interpreter symbols undefined and resolve them
//! when Python loads the module (`-undefined dynamic_lookup`). maturin passes these flags
//! itself; this script adds them so that `cargo build --workspace` also links the cdylib on
//! macOS runners. It is a no-op on Linux and Windows.

#![forbid(unsafe_code)]

fn main() {
    pyo3_build_config::add_extension_module_link_args();
}
