// Tengen-Toppa ATS-friendly single-column resume.
// All text comes from data.json as plain strings, so it is never interpreted as Typst markup.
#let d = json("data.json")
#let b = d.basics

#set document(title: b.name + " - Resume", author: b.name)
#set page(paper: "a4", margin: (x: 1.6cm, y: 1.4cm))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial", "Libertinus Serif"), size: 10pt, lang: "en")
#set par(justify: false, leading: 0.55em, spacing: 0.7em)
#set list(indent: 0.4em, body-indent: 0.5em, spacing: 0.45em)

#let section(title) = {
  v(0.5em)
  text(size: 10.5pt, weight: "bold", upper(title))
  v(-0.55em)
  line(length: 100%, stroke: 0.6pt)
  v(-0.2em)
}

#align(center)[
  #text(size: 18pt, weight: "bold", b.name)
  #if b.headline != "" { linebreak(); text(size: 10.5pt, b.headline) }
  #linebreak()
  #((b.email, b.phone, b.location).filter(x => x != "").join("  |  "))
  #if b.links.len() > 0 {
    linebreak()
    b.links.map(l => link(l.url, l.label + ": " + l.url.replace("https://", ""))).join("  |  ")
  }
]

#if d.summary != "" {
  section("Summary")
  d.summary
}

#if d.skills.len() > 0 {
  section("Skills")
  d.skills.join(", ")
}

#if d.experience.len() > 0 {
  section("Experience")
  for e in d.experience {
    grid(
      columns: (1fr, auto),
      strong(e.role + " - " + e.company) + if e.location != "" { text(fill: luma(80), "  (" + e.location + ")") },
      e.start + " - " + e.end,
    )
    v(-0.3em)
    list(..e.bullets.map(x => [#x]))
  }
}

#if d.projects.len() > 0 {
  section("Projects")
  for p in d.projects {
    let head = strong(p.name)
    if p.tech.len() > 0 { head = head + "  |  " + p.tech.join(", ") }
    if p.url != "" { head = head + "  |  " + link(p.url, p.url.replace("https://", "")) }
    head
    v(-0.3em)
    list(..p.bullets.map(x => [#x]))
  }
}

#if d.education.len() > 0 {
  section("Education")
  for ed in d.education {
    grid(
      columns: (1fr, auto),
      strong(ed.degree) + " - " + ed.institution + if ed.score != "" { ", " + ed.score },
      (ed.start, ed.end).filter(x => x != "").join(" - "),
    )
  }
}

#if d.certifications.len() > 0 {
  section("Certifications")
  list(..d.certifications.map(x => [#x]))
}

#if d.achievements.len() > 0 {
  section("Achievements")
  list(..d.achievements.map(x => [#x]))
}
