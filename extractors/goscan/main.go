// goscan - emit the cross-repo-relevant facts about a Go repo.
//
// Go needs real AST work that Python's `ast` equivalent cannot be faked with
// regexes. In Python, `from neo4j import Driver` names the symbol in the import
// statement. In Go, `import "github.com/grafana/dskit/ring"` names only the
// package; the symbol appears later as a selector expression `ring.Ring`. So
// recovering (module, symbol) pairs means resolving selector expressions
// against the file's import block, which is exactly what go/ast is for.
//
// Emits two things per repo:
//
//	refs - (file, module, symbol) for every exported symbol used from an
//	       imported package. The consumer side of a cross-repo edge.
//	defs - (file, name) for every exported top-level declaration. The
//	       publisher side, used to confirm a referenced symbol really exists.
//
// Vendored code is skipped. A vendored copy of a sibling repo would otherwise
// look like local code and silently erase the cross-repo edge - which is the
// whole failure mode this corpus exists to demonstrate.
package main

import (
	"encoding/json"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"unicode"
)

type Ref struct {
	File   string `json:"file"`
	Module string `json:"module"`
	Symbol string `json:"symbol"`
	Line   int    `json:"line"`
}

type Def struct {
	File string `json:"file"`
	Name string `json:"name"`
}

type Output struct {
	Refs  []Ref  `json:"refs"`
	Defs  []Def  `json:"defs"`
	Files int    `json:"files"`
	Root  string `json:"root"`
}

var skipDirs = map[string]bool{
	"vendor": true, "testdata": true, ".git": true,
	"node_modules": true, "dist": true, "build": true,
}

func exported(name string) bool {
	if name == "" {
		return false
	}
	return unicode.IsUpper([]rune(name)[0])
}

func main() {
	root := os.Args[1]
	out := Output{Refs: []Ref{}, Defs: []Def{}, Root: root}
	seenRef := map[Ref]bool{}

	filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if info.IsDir() {
			if skipDirs[info.Name()] {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") {
			return nil
		}

		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			return nil // unparseable file - skipped, not fatal
		}
		rel, _ := filepath.Rel(root, path)
		rel = filepath.ToSlash(rel)
		out.Files++

		// local package identifier -> full import path
		imports := map[string]string{}
		for _, imp := range file.Imports {
			p, err := strconv.Unquote(imp.Path.Value)
			if err != nil {
				continue
			}
			name := p[strings.LastIndex(p, "/")+1:]
			if imp.Name != nil {
				if imp.Name.Name == "_" || imp.Name.Name == "." {
					// Blank imports bind nothing; dot-imports bind everything
					// without a qualifier and cannot be attributed to a package
					// by selector analysis. Both are skipped rather than guessed.
					continue
				}
				name = imp.Name.Name
			}
			imports[name] = p
		}

		// Exported top-level declarations - the publisher side.
		for _, decl := range file.Decls {
			switch d := decl.(type) {
			case *ast.FuncDecl:
				if d.Recv == nil && exported(d.Name.Name) {
					out.Defs = append(out.Defs, Def{rel, d.Name.Name})
				}
			case *ast.GenDecl:
				for _, spec := range d.Specs {
					switch s := spec.(type) {
					case *ast.TypeSpec:
						if exported(s.Name.Name) {
							out.Defs = append(out.Defs, Def{rel, s.Name.Name})
						}
					case *ast.ValueSpec:
						for _, n := range s.Names {
							if exported(n.Name) {
								out.Defs = append(out.Defs, Def{rel, n.Name})
							}
						}
					}
				}
			}
		}

		// Selector expressions - the consumer side.
		ast.Inspect(file, func(n ast.Node) bool {
			sel, ok := n.(*ast.SelectorExpr)
			if !ok {
				return true
			}
			ident, ok := sel.X.(*ast.Ident)
			if !ok {
				return true
			}
			// A local variable can shadow a package name. Requiring the
			// identifier to resolve to no local object is the cheap guard;
			// full type checking would need go/types and the whole build.
			if ident.Obj != nil {
				return true
			}
			module, ok := imports[ident.Name]
			if !ok || !exported(sel.Sel.Name) {
				return true
			}
			key := Ref{File: rel, Module: module, Symbol: sel.Sel.Name}
			if !seenRef[key] {
				seenRef[key] = true
				r := key
				r.Line = fset.Position(sel.Sel.Pos()).Line
				out.Refs = append(out.Refs, r)
			}
			return true
		})
		return nil
	})

	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "")
	enc.Encode(out)
}
