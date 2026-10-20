//! Minimal POSIX-like splitting for `--cmd '<argv...>'`: whitespace separates words, single
//! quotes are literal, double quotes allow `\"` and `\\`. Outside quotes a backslash is literal
//! unless it precedes a quote character, so Windows paths such as `C:\bin\argus-next.exe`
//! survive unchanged.

/// Split a command line into words.
pub fn split(s: &str) -> Result<Vec<String>, String> {
    let mut words = Vec::new();
    let mut cur = String::new();
    let mut in_word = false;
    let mut chars = s.chars();
    while let Some(c) = chars.next() {
        match c {
            c if c.is_whitespace() => {
                if in_word {
                    words.push(std::mem::take(&mut cur));
                    in_word = false;
                }
            }
            '\'' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('\'') => break,
                        Some(ch) => cur.push(ch),
                        None => return Err("unterminated single quote".into()),
                    }
                }
            }
            '"' => {
                in_word = true;
                loop {
                    match chars.next() {
                        Some('"') => break,
                        Some('\\') => match chars.next() {
                            Some(ch @ ('"' | '\\')) => cur.push(ch),
                            Some(ch) => {
                                cur.push('\\');
                                cur.push(ch);
                            }
                            None => return Err("unterminated double quote".into()),
                        },
                        Some(ch) => cur.push(ch),
                        None => return Err("unterminated double quote".into()),
                    }
                }
            }
            '\\' => {
                in_word = true;
                match chars.clone().next() {
                    Some(q @ ('"' | '\'')) => {
                        chars.next();
                        cur.push(q);
                    }
                    _ => cur.push('\\'),
                }
            }
            ch => {
                in_word = true;
                cur.push(ch);
            }
        }
    }
    if in_word {
        words.push(cur);
    }
    Ok(words)
}

#[cfg(test)]
mod tests {
    use super::split;

    #[test]
    fn splits_words_and_quotes() {
        assert_eq!(split("  a b\tc ").unwrap(), ["a", "b", "c"]);
        assert_eq!(
            split(r#"py -m 'argus x' "q \"y\"" ''"#).unwrap(),
            ["py", "-m", "argus x", "q \"y\"", ""]
        );
        assert_eq!(split(r#""a\nb""#).unwrap(), [r"a\nb"]);
        assert!(split("").unwrap().is_empty());
    }

    #[test]
    fn keeps_windows_paths_intact() {
        assert_eq!(
            split(r"C:\bin\argus-next.exe --version").unwrap(),
            [r"C:\bin\argus-next.exe", "--version"]
        );
        assert_eq!(split(r"it\'s").unwrap(), ["it's"]);
        assert_eq!(split(r"dir\").unwrap(), [r"dir\"]);
    }

    #[test]
    fn rejects_unterminated_quotes() {
        assert!(split("'a").is_err());
        assert!(split("\"a").is_err());
    }
}
