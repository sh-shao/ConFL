package org.extractor;

import java.nio.file.Path;
import java.util.Arrays;

/** Version 2 CLI. Optional trailing arguments are dependency JARs. */
public class Main {
    public static void main(String[] args) throws Exception {
        if (args.length < 3) {
            System.err.println("Usage: java -jar facts-extractor.jar <project_id> <src_root> <facts_dir> [dependency.jar ...]");
            System.exit(1);
        }
        new SourceFacts(Path.of(args[1]), Arrays.stream(args).skip(3).map(Path::of).toList())
                .extract(Path.of(args[2]));
        System.out.println("Extracted schema v2 facts for " + args[0]);
    }
}
