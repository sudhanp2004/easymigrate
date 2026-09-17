interface StagePlaceholderProps {
  title: string
}

/** Stands in for resolve/plan/apply until plan 5 builds them against DESIGN.md. */
export function StagePlaceholder({ title }: StagePlaceholderProps) {
  return (
    <section className="page">
      <h1>{title}</h1>
      <p>This screen is not built yet.</p>
    </section>
  )
}
