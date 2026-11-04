package org.extractor;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import java.nio.file.*;
import java.util.*;
import static org.junit.jupiter.api.Assertions.*;

class SourceFactsTest {
    @TempDir Path temp;
    private Path extract(String code) throws Exception {
        Path root = temp.resolve("project"), src = root.resolve("src/main/java/p");
        Files.createDirectories(src); Files.writeString(src.resolve("Example.java"), code);
        Path out = temp.resolve("facts"); new SourceFacts(root, List.of()).extract(out); return out;
    }
    private List<String> rows(Path out, String relation) throws Exception {
        return Files.readAllLines(out.resolve(relation + ".facts"));
    }
    @Test void identitiesAndFieldSemantics() throws Exception {
        Path out = extract("""
            package p;
            class Example {
                int count; Boolean stopped; String state;
                void update(int count) { count++; }
                void update(String value) { this.count = 1; stopped = true; state = value; }
                void external(Other other) { other.count++; }
                static class Nested { void work() {} }
            }
            class Other { int count; }
            """);
        assertTrue(rows(out,"Method").contains("p.Example#update(int)"));
        assertTrue(rows(out,"Method").contains("p.Example#update(java.lang.String)"));
        assertTrue(rows(out,"Method").contains("p.Example.Nested#work()"));
        List<String> access = rows(out,"Access");
        assertFalse(access.stream().anyMatch(s -> s.startsWith("p.Example#update(int)\t")), access.toString());
        assertTrue(access.stream().anyMatch(s -> s.contains("\tp.Example#stopped\tW\t")), access.toString());
        assertTrue(access.stream().anyMatch(s -> s.contains("\tp.Example#state\tW\t")));
        assertTrue(access.stream().anyMatch(s -> s.contains("\tp.Other#count\tRW\t")));
        assertFalse(access.stream().anyMatch(s -> s.contains("\tp.Example#count\tR\t")));
    }
    @Test void asyncBodiesAndReferencesAreSeparate() throws Exception {
        Path out = extract("""
            package p;
            import java.util.concurrent.ExecutorService;
            import java.util.concurrent.CompletableFuture;
            class Example {
                int value;
                void launch(ExecutorService e) {
                    e.submit(() -> { value++; work(); });
                    e.submit(() -> value--);
                    e.submit(this::work);
                    CompletableFuture.runAsync(() -> value++);
                    new Thread(() -> value++).start();
                }
                void work() { value++; }
            }
            """);
        List<String> entries = rows(out,"ThreadEntry"), access = rows(out,"Access");
        assertEquals(5, entries.size(), entries.toString());
        assertTrue(entries.stream().anyMatch(s -> s.contains("#task@")));
        assertTrue(rows(out,"VirtualCall").stream().anyMatch(s -> s.contains("#task@") && s.endsWith("\tp.Example\twork\t()")));
        assertFalse(entries.stream().anyMatch(s -> s.contains("#launch(")));
        assertFalse(access.stream().anyMatch(s -> s.startsWith("p.Example#launch(")), access.toString());
        assertEquals(5, rows(out,"AsyncCall").size());
        assertFalse(rows(out,"VirtualCall").stream().anyMatch(s -> s.startsWith("p.Example#launch(") && s.contains("\twork\t")));
    }
    @Test void synchronizationAndVirtualDispatch() throws Exception {
        Path out = extract("""
            package p;
            class Example {
                final Object lock = new Object(); int value;
                synchronized void guarded() { value++; }
                void block() { synchronized (lock) { value = 2; } }
                void invoke(Base b) { b.work(); this.guarded(); }
            }
            class Base { void work() {} }
            class Child extends Base { @Override void work() {} }
            """);
        assertTrue(rows(out,"Guard").stream().anyMatch(s -> s.startsWith("p.Example#guarded()\t") && s.endsWith("\tp.Example#this")));
        assertTrue(rows(out,"Guard").stream().anyMatch(s -> s.startsWith("p.Example#block()\t") && s.endsWith("\tp.Example#lock")));
        assertTrue(rows(out,"VirtualCall").contains("p.Example#invoke(p.Base)\tp.Base\twork\t()"));
        assertTrue(rows(out,"VirtualCall").contains("p.Example#invoke(p.Base)\tp.Example\tguarded\t()"));
    }
    @Test void importedTypesAndUnresolvedCallsAreNotInvented() throws Exception {
        Path root = temp.resolve("project"), src = root.resolve("module/src/main/java");
        Files.createDirectories(src.resolve("a")); Files.createDirectories(src.resolve("b"));
        Files.writeString(src.resolve("b/Base.java"), "package b; public class Base { public void work(){} }");
        Files.writeString(src.resolve("a/Child.java"), "package a; import b.Base; class Child extends Base { void go(){ new Base().work(); missing(); } }");
        Path out = temp.resolve("facts"); new SourceFacts(root, List.of()).extract(out);
        assertTrue(rows(out,"Extends").contains("a.Child\tb.Base"));
        assertTrue(rows(out,"Unresolved").stream().anyMatch(s -> s.contains("missing()")));
        assertFalse(rows(out,"DirectCall").stream().anyMatch(s -> s.contains("a.Base")));
        assertEquals(rows(out,"VirtualCall").size(), new HashSet<>(rows(out,"VirtualCall")).size());
    }
    @Test void anonymousRunnableIsNotTheEnclosingClass() throws Exception {
        Path out = extract("""
            package p;
            import java.util.concurrent.ExecutorService;
            class Example {
                int value;
                void launch(ExecutorService executor) {
                    executor.submit(new Runnable() { public void run() { value++; } });
                }
                void run() { }
            }
            """);
        List<String> methods = rows(out,"Method");
        assertEquals(1, methods.stream().filter(s -> s.equals("p.Example#run()")).count());
        assertTrue(methods.stream().anyMatch(s -> s.contains("anonymous") && s.endsWith("#run()")), methods.toString());
        assertFalse(rows(out,"Access").stream().anyMatch(s -> s.startsWith("p.Example#launch(")));
        assertTrue(rows(out,"Implements").stream().anyMatch(s -> s.contains("$anonymous@") && s.endsWith("\tjava.lang.Runnable")));
        Path second = temp.resolve("facts-second");
        new SourceFacts(temp.resolve("project"), List.of()).extract(second);
        for (String relation : List.of("Method", "InClass", "DirectCall", "VirtualCall", "Access", "ThreadEntry", "AsyncCall"))
            assertEquals(rows(out, relation), rows(second, relation), relation);
    }

}
