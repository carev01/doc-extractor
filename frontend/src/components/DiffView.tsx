/** Render a unified-diff string with per-line coloring. */
export default function DiffView({ text }: { text: string }) {
  const lines = text.split("\n");
  return (
    <pre className="diff-view">
      {lines.map((line, i) => {
        let cls = "diff-line";
        if (line.startsWith("+++") || line.startsWith("---")) {
          cls += " diff-meta";
        } else if (line.startsWith("@@")) {
          cls += " diff-hunk";
        } else if (line.startsWith("+")) {
          cls += " diff-add";
        } else if (line.startsWith("-")) {
          cls += " diff-del";
        }
        return (
          <span key={i} className={cls}>
            {line || " "}
            {"\n"}
          </span>
        );
      })}
    </pre>
  );
}
