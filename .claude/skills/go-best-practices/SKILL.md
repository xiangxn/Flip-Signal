---

name: go-best-practices
description: Apply modern idiomatic Go best practices whenever writing, modifying, reviewing, or refactoring Go code. Prefer Go standard library conventions and official Go guidance. Avoid unnecessary abstractions, dependencies, complexity, and speculative optimization. This skill provides language and engineering guidance only; it does not prescribe project architecture, dependency choices, lint configurations, or workflow tooling.
--------------------------------------------------------------

# Go Best Practices

Use this skill whenever generating, modifying, reviewing, debugging, or refactoring Go code.

The goal is not merely to produce code that compiles. The goal is to produce **idiomatic, simple, maintainable, correct, and unsurprising Go code**.

Prefer the Go language and standard library's established conventions over patterns imported from other languages.

When project-specific conventions conflict with a general Go preference, preserve the existing project convention unless it is clearly incorrect, unsafe, or explicitly requested to be changed.

---

## 1. Core Principles

Prefer:

* simplicity over abstraction
* readability over cleverness
* explicit control flow over hidden behavior
* composition over inheritance-style designs
* small interfaces over large interfaces
* concrete types until an interface is actually useful
* zero values that are useful when practical
* standard library facilities over unnecessary dependencies
* deterministic behavior over implicit behavior
* explicit ownership and lifecycle
* clear error handling
* boring code over clever code

Do not introduce an abstraction merely because it is theoretically reusable.

Do not refactor working code solely to make it look different from the existing implementation.

Do not introduce a framework, dependency, interface, generic abstraction, helper layer, or design pattern unless there is a concrete reason.

---

# 2. Before Writing Code

Before generating or modifying Go code:

1. Inspect the surrounding code.
2. Understand existing package conventions.
3. Check whether an existing helper/type already solves the problem.
4. Prefer the smallest change that correctly solves the problem.
5. Preserve existing public APIs unless the task requires changing them.
6. Consider error paths, cancellation, concurrency, resource ownership, and cleanup.
7. Consider whether the zero value and nil behavior are meaningful.
8. Avoid speculative abstractions.

Do not redesign unrelated code while implementing a requested change.

---

# 3. Naming

Use idiomatic Go naming.

Prefer:

```go
type OrderBook struct{}
type OrderID string

func (o *OrderBook) BestBid() Price
```

Avoid:

```go
type OrderBookManager struct{}
type OrderBookInterface interface{}
func GetOrderBookManager() *OrderBookManager
```

unless the longer names are actually necessary.

General rules:

* use `MixedCaps` and `mixedCaps`
* avoid underscores in Go identifiers
* avoid redundant package names in identifiers
* use short, meaningful names
* use conventional abbreviations when they are well established
* prefer `ctx` for context parameters
* prefer `err` for errors
* prefer `i`, `j`, etc. for simple local loop indexes
* use descriptive names when scope or complexity requires them

Do not optimize names for brevity when doing so harms readability.

---

# 4. Packages

Package names should be:

* short
* lowercase
* meaningful
* not pluralized merely because they contain multiple files
* free of underscores and mixedCaps

Avoid names such as:

```text
common
utils
helpers
misc
manager
```

unless the package has a genuinely coherent purpose.

Do not create a new package merely to move a small amount of code.

Keep package responsibilities cohesive.

---

# 5. Functions and Methods

Prefer small functions with a clear responsibility.

Use early returns to reduce nesting:

```go
if err != nil {
    return err
}
```

Prefer:

```go
if condition {
    return err
}

doSomething()
```

over deeply nested control flow when early return makes the code clearer.

Do not split trivial code into many one-line helper functions without a readability or reuse benefit.

Avoid functions with excessive parameters. If many parameters genuinely belong together, consider a meaningful struct rather than a generic parameter object.

Do not introduce a parameter struct solely to avoid a moderately sized parameter list.

---

# 6. Receivers

Choose pointer vs value receivers intentionally.

Use a pointer receiver when:

* the method mutates the receiver
* the type contains synchronization primitives
* copying the value would be undesirable
* the type is large enough that copying is undesirable
* the type has methods whose semantics depend on identity

Use a value receiver when:

* the type is small
* copying is cheap
* the type behaves like a value
* immutability/value semantics are desirable

Keep receiver choices consistent within a type unless there is a strong reason otherwise.

Do not use pointer receivers automatically.

---

# 7. Interfaces

Interfaces are abstractions, not mandatory companions to concrete types.

Do not create:

```go
type UserService interface {
    ...
}

type UserServiceImpl struct {
    ...
}
```

merely because other languages commonly use this pattern.

Prefer defining interfaces where they are consumed:

```go
type UserReader interface {
    GetUser(ctx context.Context, id string) (*User, error)
}
```

Use interfaces when they provide a concrete benefit such as:

* decoupling a consumer from an implementation
* substituting implementations
* testing an external dependency
* defining a small behavioral contract
* allowing multiple meaningful implementations

Do not create an interface solely for mocking a type that does not need abstraction.

Prefer small interfaces.

Do not create large "god interfaces".

---

# 8. Zero Values and nil

Prefer useful zero values when practical.

For example:

```go
var m map[string]int
```

may be safely read from even though it is nil.

Remember:

* reading from a nil map is safe
* writing to a nil map panics
* ranging over a nil slice/map is safe
* appending to a nil slice is safe
* calling methods on a nil pointer may or may not be safe depending on the method

Do not unnecessarily initialize values solely because their zero value is already useful.

At the same time, do not force zero-value usability when it makes the type's invariants unclear or unsafe.

---

# 9. Errors

Treat errors as part of the API.

Always consider whether an error should be:

* returned
* wrapped
* inspected
* transformed
* intentionally ignored

Prefer contextual wrapping:

```go
return fmt.Errorf("load market %s: %w", marketID, err)
```

Use:

```go
errors.Is(err, target)
errors.As(err, &target)
```

when callers need to inspect wrapped errors.

Do not compare wrapped errors with `==` unless identity semantics are explicitly intended.

Do not discard errors without a reason.

Avoid meaningless wrapping such as:

```go
return fmt.Errorf("error: %w", err)
```

Add useful context.

Do not expose internal implementation details through public errors unless that behavior is intentional.

---

# 10. Error Design

Use sentinel errors only when callers need stable identity:

```go
var ErrNotFound = errors.New("not found")
```

Use typed errors when callers need structured information.

Do not create a custom error type for every possible failure.

Prefer the simplest error representation that provides the required behavior.

Avoid using strings as the primary mechanism for programmatic error classification.

---

# 11. Context

Use `context.Context` for request-scoped cancellation, deadlines, and values that belong to the request lifecycle.

Prefer:

```go
func (s *Service) Run(ctx context.Context) error
```

Context should normally be the first parameter.

Do not store `context.Context` inside long-lived structs unless there is a very specific, justified reason.

Do not pass nil as a context.

Use cancellation and deadlines intentionally.

A goroutine performing blocking or long-running work should normally have a clear way to observe cancellation when the operation is expected to stop.

Do not create a new background context merely to avoid dealing with cancellation.

---

# 12. Goroutines

Every goroutine should have a clear ownership and lifecycle model.

Before creating a goroutine, be able to answer:

1. Who starts it?
2. What work does it perform?
3. What causes it to stop?
4. Who waits for it?
5. What happens if the parent operation fails?
6. What happens during shutdown?

Avoid:

```go
go func() {
    for {
        ...
    }
}()
```

when there is no explicit termination mechanism.

Prefer cancellation:

```go
go func() {
    defer wg.Done()

    for {
        select {
        case <-ctx.Done():
            return
        case item := <-items:
            process(item)
        }
    }
}()
```

Do not use `time.Sleep` as a substitute for synchronization.

Be alert for goroutine leaks.

---

# 13. Channels

Use channels when communication or synchronization between goroutines is the natural model.

Do not use channels merely because they are "idiomatic Go".

Use a mutex when protecting shared state is simpler and clearer.

The owner of a channel should normally be responsible for deciding when it is closed.

Do not close a channel from the receiving side unless ownership semantics explicitly require it.

Remember:

* sending on a closed channel panics
* receiving from a closed channel returns the zero value after buffered values are drained
* receiving from a nil channel blocks forever
* sending to a nil channel blocks forever
* closing a nil channel panics
* closing an already closed channel panics

Always consider these states when channel lifecycle is non-trivial.

---

# 14. Mutexes and Shared State

Prefer the simplest synchronization mechanism that correctly protects shared state.

A mutex is often clearer than a channel when the actual requirement is:

> multiple goroutines access shared mutable state.

Protect invariants, not merely individual fields.

Avoid exposing internal mutable state without a clear ownership model.

Do not copy a value containing `sync.Mutex`, `sync.RWMutex`, or similar synchronization primitives after first use.

Consider `go test -race` when changing concurrent code.

---

# 15. sync/atomic

Use atomic operations when they clearly express the intended synchronization semantics.

Do not use atomics merely to avoid a mutex.

Keep atomic state simple and well-defined.

If several fields must change together as one invariant, a mutex or another higher-level synchronization mechanism may be more appropriate.

---

# 16. Slices

Remember that slices are descriptors over an underlying array.

Be aware of:

* shared backing arrays
* append reallocations
* aliasing
* capacity
* nil vs empty slices

Do not assume that:

```go
b := a
```

creates an independent copy.

When ownership requires an independent slice, copy it explicitly.

Avoid unnecessary copying when ownership is already clear.

Prefer straightforward slice operations over clever manual memory manipulation.

---

# 17. Maps

Maps are reference-like data structures.

A nil map can be read from but cannot be written to.

Initialize before writing:

```go
if m == nil {
    m = make(map[string]Value)
}
```

or initialize when constructing the owning object.

Do not concurrently read and write a regular map without synchronization.

Do not rely on map iteration order.

---

# 18. Strings and []byte

Avoid unnecessary conversions between `string` and `[]byte` in hot paths without evidence that they matter.

Do not optimize string/byte conversions prematurely.

Use the representation that makes the API clearest.

When dealing with binary data, prefer `[]byte`.

When dealing with immutable textual data, prefer `string`.

---

# 19. Defer

Use `defer` for resource cleanup when it improves correctness and readability:

```go
f, err := os.Open(name)
if err != nil {
    return err
}
defer f.Close()
```

Be aware that deferred calls execute when the surrounding function returns, not when the lexical block ends.

Do not put expensive or unbounded work into deferred functions without understanding the lifecycle.

Check whether cleanup functions themselves return meaningful errors when appropriate.

---

# 20. Resource Ownership

Every acquired resource should have an explicit owner and cleanup path.

Examples:

* files
* HTTP response bodies
* database rows
* network connections
* WebSocket connections
* tickers
* timers
* goroutines

For example:

```go
ticker := time.NewTicker(interval)
defer ticker.Stop()
```

Do not create timers/tickers repeatedly inside loops when their lifecycle can be managed more clearly.

---

# 21. HTTP

When using `http.Client`:

* reuse clients when appropriate
* configure timeouts intentionally
* close response bodies
* check errors
* respect context cancellation

Do not create a new `http.Client` for every request without a concrete reason.

Do not use an HTTP request without considering its timeout/cancellation behavior.

---

# 22. JSON and Serialization

Use standard `encoding/json` unless another serialization format is required.

Keep JSON tags intentional.

Do not introduce custom marshal/unmarshal logic unless it solves a real requirement.

When implementing custom marshaling, carefully consider:

* recursion
* nil behavior
* backward compatibility
* zero values
* error propagation

---

# 23. Generics

Use generics when they make reusable code substantially clearer or eliminate meaningful duplication.

Do not use generics merely because they are available.

Avoid generic abstractions that obscure simple concrete code.

Prefer concrete types when only one meaningful type is involved.

A generic helper should have a clear semantic purpose, not merely demonstrate generic syntax.

---

# 24. Standard Library First

Prefer the standard library when it provides a suitable solution.

Before adding a dependency, consider whether the standard library already provides the required functionality.

Do not add a dependency merely to save a few lines of straightforward code.

However, do not reject an existing project dependency merely because the standard library has a partial alternative.

Preserve established project dependencies unless there is a concrete reason to change them.

---

# 25. Performance

Correctness and clarity come before speculative optimization.

Do not:

* introduce `sync.Pool` without evidence
* manually optimize allocations without evidence
* complicate data structures for hypothetical performance
* replace readable code with unsafe tricks without measurement

When performance actually matters:

1. measure
2. identify the bottleneck
3. optimize the bottleneck
4. benchmark
5. verify correctness

Prefer benchmarks and profiling over intuition.

---

# 26. Unsafe

Avoid `unsafe` unless there is a concrete and demonstrated requirement.

Before using `unsafe`, consider whether:

* the standard library already provides a solution
* a normal allocation is acceptable
* the performance requirement has been measured
* the memory/lifetime invariants can be maintained safely

Do not introduce `unsafe` merely to make code shorter or "faster".

---

# 27. Reflection

Avoid reflection when normal Go constructs are sufficient.

Reflection increases complexity and weakens compile-time guarantees.

Use reflection when it is genuinely appropriate, especially for generic infrastructure or serialization mechanisms, but do not use it to avoid writing straightforward typed code.

---

# 28. Initialization

Avoid unnecessary package-level mutable state.

Be cautious with `init()`.

Prefer explicit initialization when initialization order or dependencies matter.

Do not hide significant application behavior inside package initialization.

---

# 29. Global State

Avoid mutable global state.

If global state is required, make ownership and synchronization explicit.

Do not introduce global singletons merely to avoid passing dependencies.

Prefer explicit dependency construction.

---

# 30. Concurrency-Safe API Design

When designing concurrent components, explicitly define:

* which methods are safe concurrently
* which state is protected
* who owns lifecycle
* whether callbacks execute synchronously or asynchronously
* whether ordering is guaranteed
* what happens during shutdown
* whether operations are idempotent

Do not imply concurrency safety merely because a type contains a mutex.

Concurrency semantics are part of the API contract.

---

# 31. API Design

Public APIs should be:

* small
* unsurprising
* explicit
* difficult to misuse

Avoid exposing internal implementation details.

Prefer returning the smallest useful abstraction.

Do not return internal mutable data unless ownership semantics are clear.

Avoid APIs that require callers to know undocumented ordering or lifecycle rules.

If an operation can fail, make failure visible in the API.

---

# 32. Comments and Documentation

Comments should explain **why**, not merely repeat **what** the code does.

Bad:

```go
// Increment i.
i++
```

Useful:

```go
// Retry is capped here because the upstream API can remain unavailable
// indefinitely and the caller must retain control of shutdown.
```

Exported identifiers should have useful documentation when required by Go documentation conventions.

Do not add comments that become false after future refactoring.

Prefer clear code over excessive comments.

---

# 33. Logging

Do not log the same error repeatedly at every layer without adding useful context.

A lower-level function should normally return an error; the appropriate boundary should decide whether and how to log it.

Avoid logging:

* secrets
* credentials
* private keys
* authentication tokens
* unnecessary sensitive payloads

Keep structured logging consistent with the existing project.

Do not introduce a new logging framework merely because another one exists.

---

# 34. Testing

Use Go's standard `testing` package unless the project already has a meaningful reason to use another testing framework.

Prefer tests that are:

* deterministic
* isolated
* readable
* fast
* focused on observable behavior

Table-driven tests are useful when multiple cases share the same structure, but they are not mandatory.

Do not force table-driven tests onto a test that is clearer as a simple test.

Use subtests when they improve organization or diagnostics.

Use `t.Helper()` for test helper functions.

Use `t.Cleanup()` for test-scoped cleanup where appropriate.

Avoid tests that depend on arbitrary sleeps.

Prefer deterministic synchronization.

For concurrent code, consider:

```bash
go test -race ./...
```

when the environment supports it.

---

# 35. Testing Error Paths

Do not test only successful execution.

Consider:

* invalid input
* missing resources
* dependency failure
* cancellation
* timeout
* partial failure
* repeated calls
* concurrent calls
* shutdown
* boundary values
* empty/nil input

Tests should verify externally meaningful behavior rather than internal implementation details whenever practical.

---

# 36. Formatting and Static Analysis

Generated or modified Go code should be formatted with:

```bash
gofmt
```

When available, consider:

```bash
go vet ./...
```

and the project's configured linters.

Do not impose a particular linter configuration on a project unless requested.

Do not change lint configuration merely to make the current code pass.

Do not introduce unrelated formatting churn.

---

# 37. Compatibility

Before changing a public API, consider:

* callers
* interfaces
* serialization
* persisted data
* configuration
* backwards compatibility
* semantic behavior

Do not make breaking changes merely to make an API "more idiomatic".

If a breaking change is required, make the impact explicit.

---

# 38. Refactoring

Prefer incremental refactoring.

When modifying existing code:

1. understand current behavior
2. identify the specific problem
3. make the smallest safe change
4. preserve unrelated behavior
5. run relevant tests
6. inspect the final diff

Do not combine a bug fix with an unrelated architectural rewrite.

Do not rename, reorganize, or redesign unrelated code simply because another design appears cleaner.

---

# 39. Avoid Cargo-Cult Go

Do not blindly apply rules such as:

* "always use pointers"
* "always use interfaces"
* "always use generics"
* "always use channels"
* "never use mutexes"
* "always use dependency injection"
* "always use table-driven tests"
* "always create a constructor"
* "always return pointers"
* "never return pointers"
* "always use context"
* "never use context"
* "always abstract"

Go best practices are contextual.

The objective is idiomatic, understandable code, not mechanical compliance with arbitrary rules.

---

# 40. Project-Specific Rules Take Precedence

This skill defines general Go practices.

It does **not** define:

* project architecture
* package layout
* deployment architecture
* dependency policy
* logging framework
* configuration framework
* CI system
* database layer
* trading strategy
* business rules

When the project has an explicit convention, follow it unless:

1. it is demonstrably incorrect or unsafe, or
2. the user explicitly asks to change it.

Do not "fix" an existing project simply because it differs from this skill.

---

# 41. Final Go Code Check

Before finalizing generated or modified Go code, mentally verify:

### Correctness

* Does it handle errors?
* Are nil cases understood?
* Are boundary conditions handled?
* Are resources cleaned up?
* Is cancellation handled where necessary?

### Idiomatic Go

* Is the code simple?
* Are names idiomatic?
* Are interfaces actually necessary?
* Are pointers/value receivers intentional?
* Is the zero value behavior sensible?
* Is the standard library sufficient?

### Concurrency

If concurrency is involved:

* Who owns each goroutine?
* How does each goroutine terminate?
* Who owns each channel?
* Can a channel be closed twice?
* Is shared state synchronized?
* Can shutdown race with normal operation?
* Can a goroutine leak?

### Maintainability

* Is there unnecessary abstraction?
* Is there unnecessary dependency usage?
* Is the code easy for another Go developer to understand?
* Did the change introduce unrelated complexity?

### Validation

When appropriate, format and validate the code with the project's existing tooling, especially:

```bash
gofmt
go test ./...
go vet ./...
```

For concurrency-sensitive changes, consider:

```bash
go test -race ./...
```

Do not claim tests or tools were run unless they were actually run.

---

# 42. Source of Authority

When deciding whether a Go practice is idiomatic, prefer this order:

1. Go language specification and official documentation
2. Go standard library conventions
3. Official Go guidance such as Effective Go and Code Review Comments
4. Established conventions in the current project
5. General software engineering practices
6. Personal stylistic preferences

Do not present personal preference as an official Go rule.

When a rule is contextual or debatable, explain the trade-off rather than presenting it as absolute.

---

# 43. Behavioral Requirement for the Agent

When generating Go code, apply these rules proactively.

Do not wait for the user to ask:

> "Is this idiomatic Go?"

If the generated implementation violates an important Go convention, revise it before presenting the final code.

When there are multiple reasonable Go implementations, prefer the one that is:

1. simpler
2. clearer
3. more idiomatic
4. easier to test
5. easier to reason about under concurrency
6. less dependent on unnecessary abstractions

The skill is a guardrail, not a mandate to rewrite existing code.

Always prioritize correctness and the user's explicit requirements.

# Decision Hierarchy

When Go best practices, project conventions, and task requirements conflict, use this priority order:

1. **Correctness and safety**
   - Language semantics, data integrity, concurrency safety, resource safety, and security always come first.

2. **Explicit user requirements**
   - Follow the user's current task and constraints unless they would make the code incorrect or unsafe.

3. **Existing project conventions**
   - Preserve established architecture, APIs, dependencies, naming, and patterns unless there is a concrete reason to change them.

4. **Official Go guidance**
   - Prefer the Go specification, official documentation, standard library conventions, and official Go guidance.

5. **Simplicity and idiomatic Go**
   - Prefer the simplest clear implementation with minimal abstraction and dependency.

6. **Performance and optimization**
   - Optimize only when there is evidence, a stated requirement, or a measured bottleneck.

7. **Personal or stylistic preference**
   - Never override the higher-priority rules merely because an alternative looks cleaner or more elegant.

When uncertain, preserve existing behavior and choose the smallest safe change rather than introducing a new abstraction or architectural change.