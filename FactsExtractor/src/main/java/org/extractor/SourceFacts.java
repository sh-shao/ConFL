package org.extractor;

import com.github.javaparser.*;
import com.github.javaparser.ast.*;
import com.github.javaparser.ast.body.*;
import com.github.javaparser.ast.expr.*;
import com.github.javaparser.ast.stmt.*;
import com.github.javaparser.resolution.declarations.*;
import com.github.javaparser.resolution.types.ResolvedType;
import com.github.javaparser.symbolsolver.JavaSymbolSolver;
import com.github.javaparser.symbolsolver.resolution.typesolvers.*;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.*;
import java.util.*;
import java.util.stream.Stream;

/** One parse configuration and one identity scheme for every v2 fact relation.
 * This is a source-level, class-based MAY analysis, not a sound race detector.
 */
public final class SourceFacts {
    private final Path root;
    private final JavaParser parser;
    private final Map<String, SortedSet<String>> facts = new TreeMap<>();
    private final Map<Node, String> ids = new IdentityHashMap<>();
    private final Map<String, VariableDeclarator> fields = new HashMap<>();
    private final Map<String, List<MethodDeclaration>> declarations = new HashMap<>();
    private final Map<String, List<ConstructorDeclaration>> constructors = new HashMap<>();
    private final Map<String, Set<String>> ancestorCache = new HashMap<>();
    private final List<CompilationUnit> units = new ArrayList<>();
    private final Map<CompilationUnit, List<Node>> initializerBodies = new IdentityHashMap<>();
    private static final List<String> TABLES = List.of("Class", "Extends", "Implements", "Method",
            "InClass", "MethodSig", "DispatchMethod", "DirectCall", "VirtualCall", "AsyncCall",
            "ThreadEntry", "ReentrantEntry", "EntryKind", "Access", "Guard", "Sync",
            "Location", "Unresolved", "CandidateCall", "AccessEvidence",
            // Raw syntactic facts; CKBuilder/model.py derives entries, edges and state accesses.
            "FunctionalArg", "ExternalSuper", "Annotation", "Modifier", "FieldInfo", "FieldCall");

    public SourceFacts(Path root, List<Path> jars) throws IOException {
        this.root = root.toAbsolutePath().normalize();
        if (!Files.isDirectory(this.root)) throw new IOException("Source directory does not exist: " + root);
        TABLES.forEach(t -> facts.put(t, new TreeSet<>()));
        var solver = new CombinedTypeSolver();
        solver.add(new ReflectionTypeSolver());
        SortedSet<Path> roots = new TreeSet<>();
        try (Stream<Path> walk = Files.walk(this.root)) {
            walk.filter(Files::isDirectory).filter(p -> p.endsWith(Path.of("src/main/java"))).forEach(roots::add);
        }
        // Also supports a package source root passed directly.
        roots.add(this.root);
        for (Path p : roots) solver.add(new JavaParserTypeSolver(p));
        for (Path jar : jars) solver.add(new JarTypeSolver(jar));
        parser = new JavaParser(new ParserConfiguration()
                .setLanguageLevel(ParserConfiguration.LanguageLevel.JAVA_21)
                .setSymbolResolver(new JavaSymbolSolver(solver)));
    }

    private void emit(String table, Object... cols) {
        StringJoiner row = new StringJoiner("\t");
        for (Object col : cols) row.add(String.valueOf(col).replace('\t', ' ').replace('\n', ' ').replace('\r', ' '));
        facts.get(table).add(row.toString());
    }
    private String loc(Node n) {
        Path file = n.findCompilationUnit().flatMap(CompilationUnit::getStorage)
                .map(CompilationUnit.Storage::getPath).orElse(root);
        return root.relativize(file.toAbsolutePath().normalize()).toString() + ":"
                + n.getBegin().map(p -> p.line + ":" + p.column).orElse("0:0");
    }
    private void unresolved(Node n, String kind, String detail) { emit("Unresolved", loc(n), kind, detail); }
    private static String erase(String s) {
        StringBuilder out = new StringBuilder(); int depth = 0;
        for (char c : s.toCharArray()) {
            if (c == '<') depth++;
            else if (c == '>') depth--;
            else if (depth == 0 && !Character.isWhitespace(c)) out.append(c);
        }
        return out.toString().replace("...", "[]");
    }
    private String signature(ResolvedMethodLikeDeclaration m) {
        List<String> ps = new ArrayList<>();
        for (int i = 0; i < m.getNumberOfParams(); i++) ps.add(erase(m.getParam(i).getType().describe()));
        return "(" + String.join(",", ps) + ")";
    }
    private String canonicalType(ResolvedTypeDeclaration type) {
        Node ast = type.toAst().orElse(null);
        if (ast instanceof ObjectCreationExpr o && o.getAnonymousClassBody().isPresent())
            return owner(o) + "$anonymous@" + loc(o);
        return type.getQualifiedName();
    }
    private String referenceName(ResolvedType type) {
        var ref = type.asReferenceType();
        return ref.getTypeDeclaration().map(this::canonicalType).orElse(ref.getQualifiedName());
    }
    private String resolvedId(ResolvedMethodLikeDeclaration m) {
        return canonicalType(m.declaringType()) + "#"
                + (m instanceof ResolvedConstructorDeclaration ? "<init>" : m.getName()) + signature(m);
    }
    private String typeName(TypeDeclaration<?> t) {
        return t.getFullyQualifiedName().orElseGet(() -> {
            String pkg = t.findCompilationUnit().flatMap(CompilationUnit::getPackageDeclaration)
                    .map(p -> p.getNameAsString() + ".").orElse("");
            return pkg + t.getNameAsString() + "@" + loc(t);
        });
    }
    private String owner(Node n) {
        Node p = n.getParentNode().orElse(null);
        while (p != null) {
            if (p instanceof ObjectCreationExpr o && o.getAnonymousClassBody().isPresent())
                return owner(o) + "$anonymous@" + loc(o);
            if (p instanceof TypeDeclaration<?> t) return typeName(t);
            p = p.getParentNode().orElse(null);
        }
        return "<unknown>@" + loc(n);
    }
    private String callableId(CallableDeclaration<?> c) {
        try {
            return resolvedId(c instanceof MethodDeclaration m ? m.resolve() : ((ConstructorDeclaration)c).resolve());
        } catch (RuntimeException e) {
            unresolved(c, "method-signature", e.getMessage());
            // Unresolved signatures are explicitly namespaced, never guessed as resolved facts.
            return owner(c) + "#" + (c instanceof ConstructorDeclaration ? "<init>" : c.getNameAsString())
                    + "(" + String.join(",", c.getParameters().stream().map(p -> "?" + erase(p.getTypeAsString())
                    + (p.isVarArgs() ? "[]" : "")).toList()) + ")";
        }
    }
    private void register(Node n, String id, String cls, String name, String sig) {
        ids.put(n, id);
        emit("Method", id); emit("InClass", id, cls); emit("MethodSig", id, name, sig);
        emit("Location", id, loc(n));
    }
    private void entry(String id, String kind, boolean reentrant) {
        emit("ThreadEntry", id); emit("EntryKind", id, kind);
        if (reentrant) emit("ReentrantEntry", id);
    }
    private boolean subtype(ResolvedType t, String base) {
        if (!t.isReferenceType()) return false;
        var ref = t.asReferenceType();
        if (ref.getQualifiedName().equals(base)) return true;
        try { return ref.getAllAncestors().stream().anyMatch(a -> a.getQualifiedName().equals(base)); }
        catch (RuntimeException e) { return false; }
    }

    public void extract(Path output) throws IOException {
        List<Path> files;
        try (Stream<Path> walk = Files.walk(root)) {
            files = walk.filter(Files::isRegularFile).filter(p -> p.toString().endsWith(".java"))
                    .filter(p -> {
                        for (Path part : root.relativize(p))
                            if (Set.of("test", "tests", "testing", "target", ".git").contains(part.toString())) return false;
                        return true;
                    }).sorted().toList();
        }
        String allowlist = System.getenv("CONFL_SOURCE_ALLOWLIST");
        if (allowlist != null && !allowlist.isBlank()) {
            Set<String> selected = new HashSet<>(Files.readAllLines(Path.of(allowlist), StandardCharsets.UTF_8));
            int before = files.size();
            files = files.stream().filter(p -> selected.contains(root.relativize(p).toString().replace('\\', '/'))).toList();
            System.out.println("Source prefilter: " + files.size() + "/" + before + " files selected");
        }
        if (files.isEmpty()) throw new IOException("No production Java sources under " + root);
        for (Path file : files) {
            var result = parser.parse(file);
            if (!result.isSuccessful()) {
                emit("Unresolved", root.relativize(file), "parse", result.getProblems());
                continue;
            }
            units.add(result.getResult().orElseThrow());
        }
        if (units.isEmpty()) throw new IOException("No Java source parsed successfully");
        // Every project class is known before source-level type qualification runs.
        for (CompilationUnit cu : units) {
            for (TypeDeclaration<?> t : cu.findAll(TypeDeclaration.class)) emit("Class", typeName(t));
            for (ObjectCreationExpr o : cu.findAll(ObjectCreationExpr.class))
                if (o.getAnonymousClassBody().isPresent()) emit("Class", owner(o) + "$anonymous@" + loc(o));
        }
        for (CompilationUnit cu : units) registerDeclarations(cu);
        for (CompilationUnit cu : units) {
            for (MethodDeclaration m : cu.findAll(MethodDeclaration.class)) analyze(m);
            for (ConstructorDeclaration c : cu.findAll(ConstructorDeclaration.class)) analyze(c);
            for (LambdaExpr l : cu.findAll(LambdaExpr.class)) analyze(l);
            for (Node init : initializerBodies.getOrDefault(cu, List.of())) analyze(init);
        }
        Files.createDirectories(output);
        // Marker is written last. A failed write must not leave a valid v2 marker.
        Files.deleteIfExists(output.resolve("schema-version.txt"));
        for (var table : facts.entrySet()) Files.write(output.resolve(table.getKey() + ".facts"), table.getValue(), StandardCharsets.UTF_8);
        Files.writeString(output.resolve("schema-version.txt"), "2\n", StandardCharsets.UTF_8);
        System.out.println("Parsed " + units.size() + "/" + files.size() + " files; diagnostics: " + facts.get("Unresolved").size());
    }

    private void registerDeclarations(CompilationUnit cu) {
        for (VariableDeclarator v : cu.findAll(VariableDeclarator.class))
            if (v.getParentNode().orElse(null) instanceof FieldDeclaration f) {
                String field = owner(v) + "#" + v.getNameAsString();
                fields.put(field, v);
                emit("FieldInfo", field, declaredType(v.getType(), v));
                modifiers(field, f.getModifiers());
            }
        for (ObjectCreationExpr o : cu.findAll(ObjectCreationExpr.class)) {
            if (o.getAnonymousClassBody().isEmpty()) continue;
            String cls = owner(o) + "$anonymous@" + loc(o);
            emit("Class", cls);
            try {
                var base = o.getType().resolve().asReferenceType();
                boolean iface = base.getTypeDeclaration().map(d -> d.isInterface()).orElse(false);
                emit(iface ? "Implements" : "Extends", cls, base.getQualifiedName());
            } catch (RuntimeException e) {
                unresolved(o, "anonymous-supertype", e.getMessage());
                emit("ExternalSuper", cls, qualify(o, o.getType().asString()));
            }
        }
        for (TypeDeclaration<?> t : cu.findAll(TypeDeclaration.class)) {
            String cls = typeName(t); emit("Class", cls);
            annotations(cls, t.getAnnotations());
            if (t instanceof ClassOrInterfaceDeclaration c) {
                c.getExtendedTypes().forEach(x -> {
                    try { emit("Extends", cls, x.resolve().asReferenceType().getQualifiedName()); }
                    catch (RuntimeException e) { unresolved(x, "extends", x.toString()); emit("ExternalSuper", cls, qualify(x, x.asString())); }
                });
                c.getImplementedTypes().forEach(x -> {
                    try { emit("Implements", cls, x.resolve().asReferenceType().getQualifiedName()); }
                    catch (RuntimeException e) { unresolved(x, "implements", x.toString()); emit("ExternalSuper", cls, qualify(x, x.asString())); }
                });
            }
        }
        for (CallableDeclaration<?> c : cu.findAll(CallableDeclaration.class)) {
            String id = callableId(c), cls = owner(c);
            String name = c instanceof ConstructorDeclaration ? "<init>" : c.getNameAsString();
            String sig = id.substring(id.indexOf('(', id.lastIndexOf('#')));
            register(c, id, cls, name, sig);
            annotations(id, c.getAnnotations());
            modifiers(id, c.getModifiers());
            if (c instanceof MethodDeclaration m) {
                declarations.computeIfAbsent(m.getNameAsString(), k -> new ArrayList<>()).add(m);
            }
            if (c instanceof ConstructorDeclaration k) constructors.computeIfAbsent(cls, x -> new ArrayList<>()).add(k);
            if (c instanceof MethodDeclaration m) {
                if (!m.isStatic() && !m.isPrivate() && !m.isAbstract()) emit("DispatchMethod", id);
                if (m.isPublic() && m.isStatic() && m.getType().isVoidType()
                        && name.equals("main") && sig.equals("(java.lang.String[])")) entry(id, "main", false);
                if (sig.equals("()") && (name.equals("run") || name.equals("call"))) {
                    try {
                        var type = m.resolve().declaringType();
                        String expected = name.equals("run") ? "java.lang.Runnable" : "java.util.concurrent.Callable";
                        if (type.getQualifiedName().equals(expected) || type.getAllAncestors().stream()
                                .anyMatch(a -> a.getQualifiedName().equals(expected))) entry(id, "task-candidate", true);
                    } catch (RuntimeException e) { unresolved(m, "entry-type", e.getMessage()); }
                }
            }
        }
        for (LambdaExpr l : cu.findAll(LambdaExpr.class)) {
            String id = owner(l) + "#lambda@" + loc(l) + "()";
            register(l, id, owner(l), "lambda@" + loc(l), "()");
        }
        for (TypeDeclaration<?> t : cu.findAll(TypeDeclaration.class)) initializers(cu, typeName(t), t, t.getMembers());
        for (ObjectCreationExpr o : cu.findAll(ObjectCreationExpr.class))
            o.getAnonymousClassBody().ifPresent(body -> initializers(cu, owner(o) + "$anonymous@" + loc(o), o, body));
    }

    /** Field initializers and initializer blocks become synthetic construction-phase bodies.
     * Instance code runs in each constructor that does not delegate with this(...). */
    private void initializers(CompilationUnit cu, String cls, Node type, List<BodyDeclaration<?>> members) {
        List<Node> instance = new ArrayList<>(), statics = new ArrayList<>();
        for (BodyDeclaration<?> member : members) {
            if (member instanceof InitializerDeclaration i) (i.isStatic() ? statics : instance).add(i);
            if (member instanceof FieldDeclaration f)
                for (VariableDeclarator v : f.getVariables())
                    if (v.getInitializer().isPresent()) (f.isStatic() ? statics : instance).add(v);
        }
        for (List<Node> bodies : List.of(instance, statics)) {
            if (bodies.isEmpty()) continue;
            String name = bodies == statics ? "<clinit>" : "<instance-init>", id = cls + "#" + name + "()";
            register(bodies.get(0), id, cls, name, "()");
            for (Node body : bodies) ids.put(body, id);
            initializerBodies.computeIfAbsent(cu, k -> new ArrayList<>()).addAll(bodies);
        }
        if (instance.isEmpty()) return;
        String init = cls + "#<instance-init>()";
        List<ConstructorDeclaration> constructors = members.stream().filter(m -> m instanceof ConstructorDeclaration)
                .map(m -> (ConstructorDeclaration) m).toList();
        for (ConstructorDeclaration c : constructors)
            if (c.getBody().getStatements().stream().noneMatch(s -> s instanceof ExplicitConstructorInvocationStmt e && e.isThis()))
                emit("DirectCall", ids.get(c), init);
        if (constructors.isEmpty() && type instanceof ClassOrInterfaceDeclaration c && !c.isInterface()) {
            // Implicit default constructor, so `new C()` reaches the instance initializers.
            String ctor = cls + "#<init>()";
            if (!facts.get("Method").contains(ctor)) register(c, ctor, cls, "<init>", "()");
            emit("DirectCall", ctor, init);
        }
    }
    private void annotations(String element, NodeList<AnnotationExpr> annotations) {
        for (AnnotationExpr a : annotations) emit("Annotation", element, qualify(a, a.getNameAsString()));
    }
    private void modifiers(String element, NodeList<Modifier> modifiers) {
        for (Modifier m : modifiers) emit("Modifier", element, m.getKeyword().asString());
    }
    /** Best-effort qualified spelling without symbol resolution. Unknown packages are prefixed with '?'. */
    private String qualify(Node context, String spelling) {
        String name = erase(spelling);
        while (name.endsWith("[]")) name = name.substring(0, name.length() - 2);
        if (name.isEmpty()) return "?";
        Set<String> project = sourceTypes(context, name);
        if (!project.isEmpty()) return project.iterator().next();
        var cu = context.findCompilationUnit().orElse(null);
        String head = name.contains(".") ? name.substring(0, name.indexOf('.')) : name;
        if (cu != null) for (var imp : cu.getImports())
            if (!imp.isStatic() && !imp.isAsterisk() && imp.getNameAsString().endsWith("." + head))
                return imp.getNameAsString() + name.substring(head.length());
        if (name.contains(".") && Character.isLowerCase(name.charAt(0))) return name;
        // JDK types visible through java.lang or a wildcard import can be checked without resolution.
        List<String> packages = new ArrayList<>(List.of("java.lang"));
        if (cu != null) for (var imp : cu.getImports())
            if (!imp.isStatic() && imp.isAsterisk() && imp.getNameAsString().startsWith("java")) packages.add(imp.getNameAsString());
        for (String pkg : packages) if (jdkType(pkg + "." + head)) return pkg + "." + name;
        return "?" + name;
    }
    private final Map<String, Boolean> jdkTypes = new HashMap<>();
    private boolean jdkType(String name) {
        return jdkTypes.computeIfAbsent(name, n -> {
            try { Class.forName(n, false, ClassLoader.getPlatformClassLoader()); return true; }
            catch (ClassNotFoundException | LinkageError e) { return false; }
        });
    }

    // Walk only this execution body; lambdas and nested declarations own their own bodies.
    // A nested lambda is still visited itself, so its enclosing body can record how it is passed.
    private void walk(Node n, Node body, java.util.function.Consumer<Node> f) {
        if (n != body && n instanceof LambdaExpr) { f.accept(n); return; }
        if (n != body && (n instanceof CallableDeclaration || n instanceof FieldDeclaration
                || n instanceof TypeDeclaration || n instanceof InitializerDeclaration)) return;
        f.accept(n);
        for (Node child : n.getChildNodes()) walk(child, body, f);
    }
    private void analyze(Node body) {
        String id = ids.get(body); if (id == null) return;
        if (body instanceof VariableDeclarator v) emitAccess(id, owner(v) + "#" + v.getNameAsString(), "W", v, body);
        walk(body, body, n -> {
            if (n instanceof LambdaExpr l && n != body) functional(id, l, ids.get(l));
            if (n instanceof MethodReferenceExpr r) functional(id, r, reference(r));
            if (n instanceof MethodCallExpr c) call(id, c);
            if (n instanceof ObjectCreationExpr o) {
                try { emit("DirectCall", id, resolvedId(o.resolve())); }
                catch (RuntimeException e) { unresolved(o, "constructor", e.getMessage()); candidateConstructors(id, o); }
                if (o.getAnonymousClassBody().isPresent()) {
                    String cls = owner(o) + "$anonymous@" + loc(o);
                    if (facts.get("Method").contains(cls + "#<instance-init>()")) emit("DirectCall", id, cls + "#<instance-init>()");
                    functional(id, o, cls);
                }
            }
            if (n instanceof NameExpr || n instanceof FieldAccessExpr) access(id, (Expression)n, body);
            if (n instanceof ExplicitConstructorInvocationStmt c) {
                try { emit("DirectCall", id, resolvedId(c.resolve())); }
                catch (RuntimeException e) { unresolved(c, "constructor-chain", e.getMessage()); }
            }
        });
    }
    /** Records how a lambda, method-reference adapter or anonymous class is passed; model.py classifies it. */
    private void functional(String caller, Node node, String body) {
        if (body == null) return;
        Node child = node, parent = node.getParentNode().orElse(null);
        while (parent instanceof EnclosedExpr || parent instanceof CastExpr) { child = parent; parent = parent.getParentNode().orElse(null); }
        String context = "other", callee = "-", receiver = "-", site = loc(node);
        final Node passed = child;
        if (parent instanceof MethodCallExpr c && c.getArguments().stream().anyMatch(a -> a == passed)) {
            context = "argument"; callee = c.getNameAsString(); site = loc(c);
            receiver = c.getScope().map(this::guessType).orElse(owner(c));
        } else if (parent instanceof MethodCallExpr c && c.getScope().map(s -> s == passed).orElse(false)) {
            context = "scope"; callee = c.getNameAsString(); site = loc(c);
            receiver = node instanceof ObjectCreationExpr o ? qualify(o, o.getType().asString()) : "-";
        } else if (parent instanceof ObjectCreationExpr o && o.getArguments().stream().anyMatch(a -> a == passed)) {
            context = "constructor-argument"; callee = "<init>"; site = loc(o); receiver = qualify(o, o.getType().asString());
        } else if (parent instanceof ExplicitConstructorInvocationStmt) {
            context = "constructor-argument"; callee = "<init>"; receiver = owner(parent);
        } else if (parent instanceof VariableDeclarator || parent instanceof AssignExpr) context = "assignment";
        else if (parent instanceof ReturnStmt) context = "return";
        emit("FunctionalArg", caller, body, context, callee, receiver, site);
    }
    /** Synthetic adapter body for a method reference; its call edge mirrors an ordinary call. */
    private String reference(MethodReferenceExpr ref) {
        String wrapper = owner(ref) + "#ref@" + loc(ref) + "()";
        emit("Method", wrapper); emit("InClass", wrapper, owner(ref));
        emit("MethodSig", wrapper, "ref@" + loc(ref), "()"); emit("Location", wrapper, loc(ref));
        try {
            var m = ref.resolve();
            if (m.isStatic() || m.accessSpecifier() == com.github.javaparser.ast.AccessSpecifier.PRIVATE
                    || ref.getScope().isSuperExpr()) emit("DirectCall", wrapper, resolvedId(m));
            else {
                String receiver = canonicalType(m.declaringType());
                try {
                    var type = ref.getScope().calculateResolvedType();
                    if (type.isReferenceType()) receiver = referenceName(type);
                } catch (RuntimeException ignored) { /* Type::method scope */ }
                emit("VirtualCall", wrapper, receiver, m.getName(), signature(m));
            }
        } catch (RuntimeException e) {
            unresolved(ref, "method-reference", e.getMessage());
            Expression scope = ref.getScope();
            Set<String> owners = new HashSet<>();
            if (scope.isThisExpr() || scope.isSuperExpr()) owners.addAll(ancestors(owner(ref)));
            else {
                String type = guessType(scope);
                if (!type.startsWith("?")) owners.add(type);
                owners.addAll(sourceTypes(ref, scope.toString()));
            }
            for (String o : new ArrayList<>(owners))
                for (String cls : facts.get("Class")) if (ancestors(cls).contains(o)) owners.add(cls);
            for (MethodDeclaration m : declarations.getOrDefault(ref.getIdentifier(), List.of()))
                if (owners.contains(owner(m))) emit("CandidateCall", wrapper, ids.get(m), loc(ref), "method-reference; unresolved");
        }
        return wrapper;
    }
    /** Resolved receiver type, else declared spelling of the receiver variable/field, else '?'. */
    private String guessType(Expression scope) {
        // A declared spelling qualified by a single-type import wins: without dependency JARs the
        // solver may otherwise bind an unresolvable import to a same-named wildcard-import type.
        String declared = null;
        Node ast = scope instanceof NameExpr || scope instanceof FieldAccessExpr ? declaration(scope) : null;
        if (ast instanceof VariableDeclarator v) declared = declaredType(v.getType(), v);
        else if (ast instanceof Parameter p) declared = declaredType(p.getType(), p);
        else if (scope instanceof ObjectCreationExpr o) declared = declaredType(o.getType(), o);
        if (declared != null && !declared.startsWith("?")) return declared;
        try {
            var type = scope.calculateResolvedType();
            return type.isReferenceType() ? referenceName(type) : type.describe();
        } catch (RuntimeException ignored) { }
        if (declared != null) return declared;
        // Static call through a type name.
        if (scope instanceof NameExpr ne && Character.isUpperCase(ne.getNameAsString().charAt(0))) return qualify(scope, ne.getNameAsString());
        return "?";
    }
    /** Variable declarator or parameter behind a name or this.field expression, if any. */
    private Node declaration(Expression e) {
        String name = e instanceof NameExpr ne ? ne.getNameAsString()
                : e instanceof FieldAccessExpr fa && fa.getScope().isThisExpr() ? fa.getNameAsString() : null;
        if (name == null) return null;
        try {
            Node ast = (e instanceof NameExpr ne ? ne.resolve() : ((FieldAccessExpr) e).resolve()).toAst().orElse(null);
            if (ast instanceof FieldDeclaration f) ast = f.getVariables().stream().filter(v -> v.getNameAsString().equals(name)).findFirst().orElse(null);
            if (ast instanceof VariableDeclarationExpr x) ast = x.getVariables().stream().filter(v -> v.getNameAsString().equals(name)).findFirst().orElse(null);
            if (ast instanceof VariableDeclarator || ast instanceof Parameter) return ast;
        } catch (RuntimeException ignored) { }
        return fields.get(owner(e) + "#" + name);
    }
    private String declaredType(com.github.javaparser.ast.type.Type type, Node context) {
        if (type.isPrimitiveType()) return type.asString();
        String qualified = qualify(context, type.asString());
        if (!qualified.startsWith("?")) return qualified;
        try {
            var resolved = type.resolve();
            return resolved.isReferenceType() ? referenceName(resolved) : resolved.describe();
        } catch (RuntimeException ignored) { return qualified; }
    }
    /** Any call whose receiver is a field (or a local initialized from one) is a candidate state access of that object. */
    private void fieldCall(String caller, MethodCallExpr c) {
        Expression scope = c.getScope().orElse(null);
        if (scope == null) return;
        if (scope instanceof EnclosedExpr e) scope = e.getInner();
        String field = fieldId(scope);
        if (field == null && scope instanceof NameExpr
                && declaration(scope) instanceof VariableDeclarator v && !(v.getParentNode().orElse(null) instanceof FieldDeclaration)
                && v.getInitializer().isPresent()) field = fieldId(v.getInitializer().get());
        if (field == null) return;
        emit("FieldCall", caller, field, c.getNameAsString(), guessType(scope), loc(c));
        Node body = executionBody(c);
        Node p = c.getParentNode().orElse(null);
        while (p != null && p != body) {
            if (p instanceof SynchronizedStmt s && isInside(c, s.getBody()))
                emit("Guard", caller, field + "#state", loc(c), resourceExpression(s.getExpression()));
            p = p.getParentNode().orElse(null);
        }
        if (body instanceof MethodDeclaration m && m.isSynchronized())
            emit("Guard", caller, field + "#state", loc(c), owner(m) + (m.isStatic() ? "#class" : "#this"));
    }
    private void call(String caller, MethodCallExpr c) {
        try {
            var m = c.resolve(); String name = m.getName();
            String recv = canonicalType(m.declaringType());
            if (m.isStatic() || m.accessSpecifier() == com.github.javaparser.ast.AccessSpecifier.PRIVATE
                    || c.getScope().map(Expression::isSuperExpr).orElse(false)) {
                emit("DirectCall", caller, resolvedId(m));
            } else {
                if (c.getScope().isPresent()) {
                    var type = c.getScope().get().calculateResolvedType();
                    if (type.isReferenceType()) recv = referenceName(type);
                } else {
                    // An unqualified call in an inner/anonymous class may bind to an enclosing class method.
                    String self = owner(c);
                    recv = ancestors(self).contains(recv) ? self : recv;
                }
                emit("VirtualCall", caller, recv, name, signature(m));
            }
            boolean executor = m.declaringType().getQualifiedName().equals("java.util.concurrent.Executor")
                    || m.declaringType().getAllAncestors().stream()
                    .anyMatch(a -> a.getQualifiedName().equals("java.util.concurrent.Executor"));
            if (executor && Set.of("submit", "execute", "schedule", "scheduleAtFixedRate", "scheduleWithFixedDelay").contains(name)
                    && !c.getArguments().isEmpty()) task(caller, c.getArgument(0), c, "executor");
            if (m.declaringType().getQualifiedName().equals("java.util.concurrent.CompletableFuture")
                    && Set.of("runAsync", "supplyAsync").contains(name) && !c.getArguments().isEmpty())
                task(caller, c.getArgument(0), c, "completable-future");
            if (name.equals("start") && m.declaringType().getQualifiedName().equals("java.lang.Thread")) {
                Expression scope = c.getScope().orElse(null);
                if (scope instanceof ObjectCreationExpr o && o.getType().getNameAsString().equals("Thread") && !o.getArguments().isEmpty())
                    task(caller, o.getArgument(0), c, "thread");
                else unresolved(c, "thread-target", "Thread variable/subclass target requires allocation tracking");
            }
            if (Set.of("wait", "notify", "notifyAll", "join", "await", "signal", "signalAll", "lock", "unlock", "tryLock", "get").contains(name)) {
                String decl = m.declaringType().getQualifiedName();
                if (decl.equals("java.lang.Object") || decl.equals("java.lang.Thread") || decl.startsWith("java.util.concurrent."))
                    emit("Sync", caller, c.getScope().map(this::resourceExpression).orElse(owner(c) + "#this"), name, loc(c));
            }
            // Collection contents are distinct from reading the collection reference.
            if (c.getScope().isPresent()) {
                var scope = c.getScope().get(); var type = scope.calculateResolvedType();
                if (subtype(type, "java.util.Map") || subtype(type, "java.util.Collection")) {
                    String field = fieldId(scope);
                    if (field != null) {
                        String action = Set.of("put", "putAll", "putIfAbsent", "remove", "removeAll", "removeIf", "clear", "add", "addAll", "set", "replace", "replaceAll", "compute", "computeIfAbsent", "computeIfPresent", "merge", "retainAll").contains(name) ? "RW" : "R";
                        emitAccess(caller, field + "#contents", action, c, executionBody(c));
                    }
                }
            }
        } catch (RuntimeException e) {
            unresolved(c, "call", c + ": " + e.getMessage());
            candidateCalls(caller, c);
        }
        collectionFallback(caller, c);
        fieldCall(caller, c);
    }

    // Explicitly uncertain source candidates: never relabel these as DirectCall.
    private Set<String> sourceTypes(Node context, String spelling) {
        String name = erase(spelling);
        Set<String> possible = new TreeSet<>();
        if (facts.get("Class").contains(name)) possible.add(name);
        var cu = context.findCompilationUnit().orElseThrow();
        String pkg = cu.getPackageDeclaration().map(x -> x.getNameAsString() + ".").orElse("");
        if (facts.get("Class").contains(pkg + name)) possible.add(pkg + name);
        for (var imp : cu.getImports()) {
            if (imp.isStatic()) continue;
            String full = imp.isAsterisk() ? imp.getNameAsString() + "." + name : imp.getNameAsString();
            if ((imp.isAsterisk() || full.endsWith("." + name)) && facts.get("Class").contains(full)) possible.add(full);
        }
        String enclosing = owner(context);
        while (enclosing.contains(".")) {
            if (facts.get("Class").contains(enclosing + "." + name)) possible.add(enclosing + "." + name);
            enclosing = enclosing.substring(0, enclosing.lastIndexOf('.'));
        }
        return possible;
    }
    private Set<String> ancestors(String type) {
        if (ancestorCache.containsKey(type)) return ancestorCache.get(type);
        Set<String> found = new HashSet<>(); found.add(type);
        boolean changed;
        do {
            changed = false;
            for (String relation : List.of("Extends", "Implements"))
                for (String row : facts.get(relation)) {
                    String[] pair = row.split("\t");
                    if (found.contains(pair[0])) changed |= found.add(pair[1]);
                }
        } while (changed);
        ancestorCache.put(type, found);
        return found;
    }
    private void candidateCalls(String caller, MethodCallExpr call) {
        Set<String> receivers = new HashSet<>();
        boolean staticScope = false;
        if (call.getScope().isEmpty()) receivers.addAll(enclosingTypes(call));
        else if (call.getScope().get().isThisExpr()) receivers.add(owner(call));
        else {
            Expression scope = call.getScope().get();
            try {
                var type = scope.calculateResolvedType();
                if (type.isReferenceType()) receivers.add(referenceName(type));
            } catch (RuntimeException ignored) { }
            if (receivers.isEmpty() && scope instanceof NameExpr ne) {
                // A resolved value shadows a class name, even if its type is unavailable.
                boolean value = false;
                try {
                    var declaration = ne.resolve(); value = true;
                    Node ast = declaration.toAst().orElse(null);
                    if (ast instanceof VariableDeclarator v) receivers.addAll(sourceTypes(v, v.getTypeAsString()));
                    else if (ast instanceof Parameter p) receivers.addAll(sourceTypes(p, p.getTypeAsString()));
                } catch (RuntimeException ignored) { }
                if (!value) { receivers.addAll(sourceTypes(call, ne.getNameAsString())); staticScope = true; }
            }
            if (receivers.isEmpty() && scope instanceof FieldAccessExpr)
                { receivers.addAll(sourceTypes(call, scope.toString())); staticScope = !receivers.isEmpty(); }
        }
        Set<String> owners = new HashSet<>();
        for (String recv : receivers) {
            owners.addAll(ancestors(recv));
            if (!staticScope) for (String cls : facts.get("Class"))
                if (ancestors(cls).contains(recv)) owners.addAll(ancestors(cls));
        }
        for (MethodDeclaration m : declarations.getOrDefault(call.getNameAsString(), List.of())) {
            if (!owners.contains(owner(m)) || !m.getNameAsString().equals(call.getNameAsString())) continue;
            if (staticScope && !m.isStatic()) continue;
            if (m.isPrivate() && !enclosingTypes(call).contains(owner(m))) continue;
            int arity = m.getParameters().size();
            boolean varargs = arity > 0 && m.getParameter(arity - 1).isVarArgs();
            if (call.getArguments().size() != arity && !(varargs && call.getArguments().size() >= arity - 1)) continue;
            emit("CandidateCall", caller, ids.get(m), loc(call), "source-owner-name-arity; unresolved argument types");
        }
    }
    /** Lexically enclosing classes, innermost first; unqualified calls may bind to any of them. */
    private List<String> enclosingTypes(Node n) {
        List<String> types = new ArrayList<>();
        for (Node p = n.getParentNode().orElse(null); p != null; p = p.getParentNode().orElse(null)) {
            if (p instanceof ObjectCreationExpr o && o.getAnonymousClassBody().isPresent()) types.add(owner(o) + "$anonymous@" + loc(o));
            else if (p instanceof TypeDeclaration<?> t) {
                types.add(typeName(t));
                if (t.isStatic()) break; // a static nested class has no enclosing instance
            }
        }
        return types;
    }
    private void candidateConstructors(String caller, ObjectCreationExpr o) {
        int args = o.getArguments().size();
        for (String type : sourceTypes(o, o.getType().getNameWithScope())) {
            List<ConstructorDeclaration> ctors = constructors.getOrDefault(type, List.of());
            if (ctors.isEmpty() && args == 0 && facts.get("Method").contains(type + "#<init>()"))
                emit("CandidateCall", caller, type + "#<init>()", loc(o), "source-type-implicit-constructor; unresolved");
            for (ConstructorDeclaration c : ctors) {
                int arity = c.getParameters().size();
                boolean varargs = arity > 0 && c.getParameter(arity - 1).isVarArgs();
                if (args == arity || (varargs && args >= arity - 1))
                    emit("CandidateCall", caller, ids.get(c), loc(o), "source-type-constructor-arity; unresolved argument types");
            }
        }
    }
    private void collectionFallback(String caller, MethodCallExpr call) {
        if (call.getScope().isEmpty()) return;
        String resource = fieldId(call.getScope().get());
        VariableDeclarator field = fields.get(resource);
        if (field == null) return;
        String type = erase(field.getTypeAsString());
        Set<String> containers = Set.of("Collection", "List", "Set", "Map", "SortedSet", "SortedMap",
                "NavigableSet", "NavigableMap", "Queue", "Deque", "HashSet", "HashMap", "ArrayList",
                "LinkedList", "TreeSet", "TreeMap", "LinkedHashSet", "LinkedHashMap");
        String simple = type.substring(type.lastIndexOf('.') + 1);
        if (!containers.contains(simple)) return;
        boolean standard = type.equals("java.util." + simple);
        if (!type.contains(".")) {
            // Do not mistake a project-defined Set/Map for the JDK container.
            if (!sourceTypes(field, type).isEmpty()) return;
            standard = field.findCompilationUnit().orElseThrow().getImports().stream().anyMatch(i ->
                    !i.isStatic() && ((i.isAsterisk() && i.getNameAsString().equals("java.util"))
                    || (!i.isAsterisk() && i.getNameAsString().equals("java.util." + simple))));
        }
        if (!standard) return;
        String name = call.getNameAsString();
        Set<String> writes = Set.of("add", "addAll", "put", "putAll", "putIfAbsent", "remove", "removeAll",
                "removeIf", "clear", "set", "replace", "replaceAll", "compute", "computeIfAbsent",
                "computeIfPresent", "merge", "retainAll", "offer", "poll", "push", "pop");
        Set<String> reads = Set.of("contains", "containsKey", "containsValue", "get", "getOrDefault", "size", "isEmpty", "iterator", "entrySet", "keySet", "values", "peek");
        if (!writes.contains(name) && !reads.contains(name)) return;
        String action = writes.contains(name) ? "RW" : "R";
        emitAccess(caller, resource + "#contents", action, call, executionBody(call));
        emit("AccessEvidence", caller, resource + "#contents", loc(call), "declared-java.util-container; operation-summary");
    }
    private Node executionBody(Node n) {
        Node p = n;
        while (p != null) {
            if (ids.containsKey(p)) return p;
            p = p.getParentNode().orElse(null);
        }
        return n;
    }
    private void task(String caller, Expression arg, Node site, String kind) {
        Expression task = arg;
        if (arg instanceof EnclosedExpr e) task = e.getInner();
        // A task held in a variable/field: use its lambda, reference or anonymous-class initializer.
        if ((task instanceof NameExpr || task instanceof FieldAccessExpr)
                && declaration(task) instanceof VariableDeclarator v && v.getInitializer().isPresent()) {
            Expression init = v.getInitializer().get();
            if (init instanceof LambdaExpr || init instanceof MethodReferenceExpr
                    || (init instanceof ObjectCreationExpr o && o.getAnonymousClassBody().isPresent())) task = init;
        }
        String target = null;
        if (task instanceof LambdaExpr) target = ids.get(task);
        else if (task instanceof MethodReferenceExpr ref) {
            try {
                var m = ref.resolve();
                String wrapper = owner(site) + "#task@" + loc(site) + "()";
                registerTask(wrapper, caller, site, kind);
                if (m.isStatic() || m.accessSpecifier() == com.github.javaparser.ast.AccessSpecifier.PRIVATE
                        || ref.getScope().isSuperExpr()) emit("DirectCall", wrapper, resolvedId(m));
                else {
                    String receiver = canonicalType(m.declaringType());
                    var type = ref.getScope().calculateResolvedType();
                    if (type.isReferenceType()) receiver = referenceName(type);
                    emit("VirtualCall", wrapper, receiver, m.getName(), signature(m));
                }
                return;
            } catch (RuntimeException e) { unresolved(ref, "task-reference", e.getMessage()); return; }
        }
        if (target != null) {
            // External references have no project body; diagnose rather than invent a Method row.
            if (!facts.get("Method").contains(target)) { unresolved(site, "external-task", target); return; }
            entry(target, kind, true); emit("AsyncCall", caller, target, loc(site));
        } else {
            // Named task objects: resolve run/call through the same virtual dispatch relation.
            try {
                var type = task.calculateResolvedType();
                String name = subtype(type, "java.util.concurrent.Callable") ? "call" : "run";
                if (!subtype(type, "java.util.concurrent.Callable") && !subtype(type, "java.lang.Runnable")) {
                    unresolved(site, "task-target", arg.toString()); return;
                }
                String wrapper = owner(site) + "#task@" + loc(site) + "()";
                registerTask(wrapper, caller, site, kind);
                emit("VirtualCall", wrapper, referenceName(type), name, "()");
            } catch (RuntimeException e) { unresolved(site, "task-target", arg.toString()); }
        }
    }
    private void registerTask(String wrapper, String caller, Node site, String kind) {
        emit("Method", wrapper); emit("InClass", wrapper, owner(site));
        emit("MethodSig", wrapper, "task@" + loc(site), "()"); emit("Location", wrapper, loc(site));
        entry(wrapper, kind, true); emit("AsyncCall", caller, wrapper, loc(site));
    }
    private String fieldId(Expression e) {
        try {
            ResolvedValueDeclaration v;
            if (e instanceof NameExpr x) v = x.resolve();
            else if (e instanceof FieldAccessExpr x) v = x.resolve();
            else return null;
            if (!v.isField()) return null;
            var f = v.asField();
            return canonicalType(f.declaringType()) + "#" + f.getName();
        } catch (RuntimeException ex) {
            unresolved(e, "value-resolution", e.toString());
            return null;
        }
    }
    private String resourceExpression(Expression e) {
        String field = fieldId(e);
        if (field != null) return field;
        if (e instanceof ThisExpr) return owner(e) + "#this";
        if (e instanceof ClassExpr c) {
            try { return c.getType().resolve().describe() + "#class"; }
            catch (RuntimeException ignored) { /* retain unknown location */ }
        }
        return "unknown@" + loc(e) + ":" + e;
    }
    private void access(String method, Expression e, Node body) {
        String field = fieldId(e); if (field == null) return;
        String action = "R";
        Node parent = e.getParentNode().orElse(null);
        if (parent instanceof AssignExpr a && a.getTarget() == e)
            action = a.getOperator() == AssignExpr.Operator.ASSIGN ? "W" : "RW";
        else if (parent instanceof UnaryExpr u && Set.of(UnaryExpr.Operator.POSTFIX_INCREMENT,
                UnaryExpr.Operator.PREFIX_INCREMENT, UnaryExpr.Operator.POSTFIX_DECREMENT,
                UnaryExpr.Operator.PREFIX_DECREMENT).contains(u.getOperator())) action = "RW";
        else if (parent instanceof ArrayAccessExpr a && a.getName() == e) {
            // The array reference is read; the element slot is a separate resource.
            Node slot = a.getParentNode().orElse(null);
            String element = "R";
            if (slot instanceof AssignExpr x && x.getTarget() == a)
                element = x.getOperator() == AssignExpr.Operator.ASSIGN ? "W" : "RW";
            else if (slot instanceof UnaryExpr u && u.getOperator() != UnaryExpr.Operator.LOGICAL_COMPLEMENT
                    && u.getOperator() != UnaryExpr.Operator.MINUS && u.getOperator() != UnaryExpr.Operator.PLUS
                    && u.getOperator() != UnaryExpr.Operator.BITWISE_COMPLEMENT) element = "RW";
            emitAccess(method, field + "#elements", element, a, body);
        }
        emitAccess(method, field, action, e, body);
    }
    private void emitAccess(String method, String resource, String action, Node site, Node body) {
        emit("Access", method, resource, action, loc(site));
        Node p = site.getParentNode().orElse(null);
        while (p != null) {
            if (p instanceof SynchronizedStmt s && isInside(site, s.getBody()))
                emit("Guard", method, resource, loc(site), resourceExpression(s.getExpression()));
            if (p == body) break;
            p = p.getParentNode().orElse(null);
        }
        if (body instanceof MethodDeclaration m && m.isSynchronized())
            emit("Guard", method, resource, loc(site), owner(m) + (m.isStatic() ? "#class" : "#this"));
    }
    private boolean isInside(Node site, Node container) {
        Node p = site;
        while (p != null) { if (p == container) return true; p = p.getParentNode().orElse(null); }
        return false;
    }
}
