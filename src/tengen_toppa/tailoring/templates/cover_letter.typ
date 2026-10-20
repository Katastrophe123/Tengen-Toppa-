#let d = json("cover_letter.json")
#let b = d.basics
#set document(title: b.name + " - Cover Letter", author: b.name)
#set page(paper: "a4", margin: (x: 2.2cm, y: 2cm))
#set text(font: ("Helvetica Neue", "Helvetica", "Arial", "Libertinus Serif"), size: 10.5pt, lang: "en")
#set par(justify: false, leading: 0.65em, spacing: 1.1em)

#text(size: 16pt, weight: "bold", b.name) \
#((b.email, b.phone, b.location).filter(x => x != "").join("  |  "))

#v(1em)
#d.date \
#d.company

#v(0.6em)
#d.greeting

#for p in d.paragraphs [
  #p

]

#d.closing \
#b.name
