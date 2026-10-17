const STEPS = [
  { n: 1, title: "Upload a CSV", text: "Drop in a table. Add a related one and foreign keys are checked too." },
  { n: 2, title: "Run the checks", text: "Deterministic checks always run. Switch on AI review for a deeper pass, with emails and phones masked first." },
  { n: 3, title: "Review the scorecard", text: "A 0 to 100 score, every flagged row with the reason, and an Agree or Disagree on each." },
];

export function HowItWorks() {
  return (
    <section className="steps" aria-label="How it works">
      {STEPS.map((s) => (
        <div key={s.n} className="card step fade-in">
          <div className="step-num">{s.n}</div>
          <h3>{s.title}</h3>
          <p>{s.text}</p>
        </div>
      ))}
    </section>
  );
}
