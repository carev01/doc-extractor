/** Renders a source's url_template with its placeholders called out.
 *
 * Without this a template reads as a malformed URL — particularly `{rev}`, which
 * nobody types and nobody can predict: it is the vendor's own per-release build
 * token, resolved automatically before each run. Showing which parts are
 * substituted, and by whom, is the difference between "this is broken" and
 * "this is a shape".
 */
const TITLES: Record<string, string> = {
  "{version}": "The product version — you set this when you bump.",
  "{rev}":
    "A per-release token the vendor mints (e.g. a build id). Resolved automatically from the vendor before each run — never typed, never stored resolved.",
};

export default function UrlTemplate({ value }: { value: string }) {
  return (
    <code className="url-template">
      {value.split(/(\{version\}|\{rev\})/g).map((part, i) =>
        TITLES[part] ? (
          <span
            key={i}
            className={`tok ${part === "{rev}" ? "tok-rev" : "tok-version"}`}
            title={TITLES[part]}
          >
            {part}
          </span>
        ) : (
          part
        ),
      )}
    </code>
  );
}
