//! A deliberately small JSON path subset for assertions on produced JSON files:
//! `$` (root), `.key`, `["key"]` and `[index]`, in any combination.

use serde_json::Value;

/// One step of a parsed path.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Segment {
    Key(String),
    Index(usize),
}

/// Parse a path such as `$.steps[0]["test file"]`.
pub fn parse(path: &str) -> Result<Vec<Segment>, String> {
    let rest = path
        .strip_prefix('$')
        .ok_or_else(|| "JSON path must start with `$`".to_owned())?;
    let chars: Vec<char> = rest.chars().collect();
    let mut out = Vec::new();
    let mut i = 0;
    while i < chars.len() {
        match chars[i] {
            '.' => {
                let start = i + 1;
                let mut j = start;
                while j < chars.len() && chars[j] != '.' && chars[j] != '[' {
                    j += 1;
                }
                if j == start {
                    return Err(format!("empty key at offset {start} in {path:?}"));
                }
                out.push(Segment::Key(chars[start..j].iter().collect()));
                i = j;
            }
            '[' => {
                let close = chars[i..]
                    .iter()
                    .position(|&c| c == ']')
                    .map(|p| p + i)
                    .ok_or_else(|| format!("unclosed `[` in {path:?}"))?;
                let inner: String = chars[i + 1..close].iter().collect();
                if let Some(quoted) = inner
                    .strip_prefix('"')
                    .and_then(|s| s.strip_suffix('"'))
                    .filter(|_| inner.len() >= 2)
                {
                    out.push(Segment::Key(quoted.to_owned()));
                } else {
                    let n: usize = inner
                        .parse()
                        .map_err(|_| format!("bad index {inner:?} in {path:?}"))?;
                    out.push(Segment::Index(n));
                }
                i = close + 1;
            }
            c => return Err(format!("unexpected {c:?} in {path:?}")),
        }
    }
    Ok(out)
}

/// Resolve a parsed path; `None` when any segment is missing.
pub fn resolve<'a>(value: &'a Value, path: &[Segment]) -> Option<&'a Value> {
    path.iter().try_fold(value, |v, seg| match seg {
        Segment::Key(k) => v.as_object()?.get(k),
        Segment::Index(n) => v.as_array()?.get(*n),
    })
}

/// The type name used by `type` assertions.
pub fn type_name(v: &Value) -> &'static str {
    match v {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parses_supported_forms() {
        assert_eq!(parse("$").unwrap(), vec![]);
        assert_eq!(
            parse(r#"$.a[2]["b c"].d"#).unwrap(),
            vec![
                Segment::Key("a".into()),
                Segment::Index(2),
                Segment::Key("b c".into()),
                Segment::Key("d".into())
            ]
        );
    }

    #[test]
    fn rejects_malformed_paths() {
        for p in ["a.b", "$.", "$..a", "$[x]", "$[1", "$a", r#"$["]"#] {
            assert!(parse(p).is_err(), "{p}");
        }
    }

    #[test]
    fn resolves_values_and_reports_missing() {
        let v = json!({"runs": [{"status": "pass", "n": 3}], "k": null});
        let p = parse("$.runs[0].status").unwrap();
        assert_eq!(resolve(&v, &p), Some(&json!("pass")));
        assert_eq!(resolve(&v, &parse("$.runs[1]").unwrap()), None);
        assert_eq!(resolve(&v, &parse("$.runs.status").unwrap()), None);
        assert_eq!(resolve(&v, &parse("$.k").unwrap()), Some(&Value::Null));
        assert_eq!(
            type_name(resolve(&v, &parse("$.runs").unwrap()).unwrap()),
            "array"
        );
    }
}
