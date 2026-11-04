package demo;

import java.util.concurrent.ExecutorService;

/** Analysis fixture; deliberately contains unsafe accesses. Do not use as application code. */
public class Example {
    int count;
    String state;
    final Object lock = new Object();

    public void launch(ExecutorService executor) {
        executor.submit(() -> { count++; state = "running"; });
        executor.submit(this::read);
        executor.submit(new Runnable() { public void run() { count--; } });
    }

    public int read() { return count; }
    public void update(int count) { count++; } // local parameter, not a field access
    public void update(String value) { state = value; }
    public synchronized void guarded() { count++; }
    public void blockGuarded() { synchronized (lock) { count++; } }
}
