<?php
declare(strict_types=1);
use Illuminate\Foundation\Http\FormRequest;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route as RouteFacade;
// Reflect a Laravel application's own validators for every mounted route.
//
//     php reflect_requests.php <bootstrap.php> [attribution.json] > reflected.json
//
// <bootstrap.php> is included and must RETURN the application instance, which
// is exactly what a stock `bootstrap/app.php` does; a consumer whose oracle
// builds the application differently wraps that in a one-line file that
// returns it. No database is touched: route loading and rules() evaluation
// are pure. The script walks the route table, reflects each controller
// method's parameters for FormRequest subclasses, instantiates the request
// against a synthetic request bound to the route, and records rules() as the
// framework's own ValidationRuleParser would see them.
//
// Output: a JSON object keyed by route name (or "METHOD uri" for unnamed
// routes; a repeated key gets "#2", "#3", ...) ->
//   {uri, methods, action, name, middleware, parameters, source, validator,
//    request_class, request_classes, rules, unresolvable,
//    rules_depend_on_request, reflection_error, request_file?, rules_declared_in?}
//
// `validator` is one of: none (no FormRequest parameter), closure (the route
// action is a closure), rules (a FormRequest with rules), empty (a FormRequest
// whose rules() is empty), rules-failed (rules() threw; see reflection_error),
// reflection-failed (the controller class or method does not exist).
//
// Rules that are closures, or objects without a string form, cannot be turned
// into a value mechanically: the field keeps the object's class name and is
// marked unresolvable. Framework rule objects that stringify (Rule::in,
// Rule::exists, Rule::unique, ...) are recorded in their string form together
// with their class. Rules\Enum is expanded to its enum cases by reflection.
//
// rules() is evaluated once, with empty input and unbound route parameters; a
// rules() body that branches on $this->... is flagged (rules_depend_on_request)
// because only the empty-input branch is recorded.
//
// ATTRIBUTION (optional). Laravel does not remember which file declared a
// route. When a consumer needs `source` = {file, ordinal, mounted, ...} per
// route -- to join the reflection back to a static inventory in declaration
// order -- it passes a JSON list of the route files in the order the
// application mounts them:
//   [{"file": "routes/api.php", "prefix": "api", "middleware": "api",
//     "namespace": "App", "mounted": true, ...any extra keys...}, ...]
// Each file is re-registered under a throwaway prefix, in that order, and the
// routes it appended are read off; they are joined back to the live table by
// (methods, uri with the prefix stripped, action). Extra keys are carried
// into `source` verbatim. Without the argument `source` is null everywhere.
if (!isset($argv[1])) {
    fwrite(STDERR, "usage: php reflect_requests.php <bootstrap.php> [attribution.json]\n");
    exit(2);
}
$app = require $argv[1];
if (!is_object($app) || !method_exists($app, 'make') || !method_exists($app, 'basePath')) {
    fwrite(STDERR, "reflect_requests: {$argv[1]} did not return a Laravel application instance\n");
    exit(2);
}
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();

$base = rtrim($app->basePath(), '/');
$router = $app['router'];

// --- 1. Attribute every mounted route to the file and ordinal that declared it.
$attribution = [];
if (isset($argv[2])) {
    $files = json_decode(file_get_contents($argv[2]), true, 512, JSON_THROW_ON_ERROR);
    if (!is_array($files)) {
        fwrite(STDERR, "reflect_requests: {$argv[2]} must hold a JSON list of route files\n");
        exit(2);
    }
    foreach ($files as $index => $spec) {
        $marker = "__reflect__/$index";
        $before = count($router->getRoutes()->getRoutes());
        $prefix = trim((string) ($spec['prefix'] ?? ''), '/');
        $group = RouteFacade::prefix($prefix === '' ? $marker : "$marker/$prefix");
        if (!empty($spec['middleware'])) {
            $group = $group->middleware($spec['middleware']);
        }
        if (!empty($spec['namespace'])) {
            $group = $group->namespace($spec['namespace']);
        }
        $path = $spec['file'];
        $group->group(str_starts_with($path, '/') ? $path : $base . '/' . $path);
        $all = $router->getRoutes()->getRoutes();
        $ordinal = 0;
        foreach (array_slice($all, $before) as $route) {
            $uri = preg_replace('#^' . preg_quote($marker, '#') . '/?#', '', $route->uri());
            $key = implode(',', $route->methods()) . ' ' . $uri . ' ' . $route->getActionName();
            $source = $spec;
            $source['ordinal'] = $ordinal++;
            $source['mounted'] = array_key_exists('mounted', $spec) ? (bool) $spec['mounted'] : true;
            $attribution[$key] = $source;
        }
    }
    $router->getRoutes()->refreshNameLookups();
    $router->getRoutes()->refreshActionLookups();
}

// --- 2. Reflect each live route.
function ruleToJson($rule): array {
    if ($rule instanceof Closure) {
        return ['class' => 'Closure', 'unresolvable' => true];
    }
    if (is_string($rule)) {
        return ['rule' => trim($rule)];
    }
    if (is_object($rule)) {
        $class = get_class($rule);
        if ($rule instanceof Illuminate\Validation\Rules\Enum) {
            $prop = new ReflectionProperty($rule, 'type');
            $prop->setAccessible(true);
            $enum = $prop->getValue($rule);
            $cases = [];
            if (is_string($enum) && enum_exists($enum)) {
                foreach ($enum::cases() as $case) {
                    $cases[] = $case instanceof BackedEnum ? $case->value : $case->name;
                }
                return ['rule' => 'in:' . implode(',', $cases), 'class' => $class, 'enum' => $enum];
            }
            return ['class' => $class, 'enum' => is_string($enum) ? $enum : null, 'unresolvable' => true];
        }
        if (method_exists($rule, '__toString')) {
            try {
                $string = (string) $rule;
            } catch (Throwable $e) {
                return ['class' => $class, 'unresolvable' => true, 'error' => get_class($e) . ': ' . $e->getMessage()];
            }
            if (str_starts_with($class, 'Illuminate\\') && $string !== '') {
                return ['rule' => $string, 'class' => $class];
            }
            return ['class' => $class, 'string' => $string, 'unresolvable' => true];
        }
        return ['class' => $class, 'unresolvable' => true];
    }
    return ['rule' => json_encode($rule), 'unresolvable' => true];
}

function explodeRules($raw): array {
    // Mirror Illuminate\Validation\ValidationRuleParser::explodeExplicitRule:
    // strings split on '|', arrays are taken element by element.
    if (is_string($raw)) {
        return array_values(array_filter(array_map('trim', explode('|', $raw)), fn ($r) => $r !== ''));
    }
    if ($raw instanceof Closure || is_object($raw)) {
        return [$raw];
    }
    return array_values((array) $raw);
}

function sourceOf(ReflectionMethod $method): string {
    $file = $method->getFileName();
    if (!$file || !is_file($file)) {
        return '';
    }
    $lines = file($file);
    return implode('', array_slice($lines, $method->getStartLine() - 1, $method->getEndLine() - $method->getStartLine() + 1));
}

$classCache = [];
function reflectRequestClass(string $class, $route, string $uri, string $method, string $base): array {
    global $classCache;
    $cacheKey = $class . '|' . $uri;
    if (isset($classCache[$cacheKey])) {
        return $classCache[$cacheKey];
    }
    $reflection = new ReflectionClass($class);
    $out = [
        'class' => $class,
        'file' => $reflection->getFileName() ? str_replace($base . '/', '', $reflection->getFileName()) : null,
        'declares_rules' => $reflection->hasMethod('rules') && $reflection->getMethod('rules')->getDeclaringClass()->getName() === $class,
        'rules_declared_in' => $reflection->hasMethod('rules') ? $reflection->getMethod('rules')->getDeclaringClass()->getName() : null,
        'rules_depend_on_request' => false,
        'rules' => [],
        'unresolvable' => [],
        'error' => null,
    ];
    if ($reflection->hasMethod('rules')) {
        $source = sourceOf($reflection->getMethod('rules'));
        $out['rules_depend_on_request'] = (bool) preg_match('/\$this->(?!rules\b)[a-zA-Z_]+\s*\(/', $source);
    }
    try {
        $request = Request::create('/' . ltrim($uri, '/'), $method, [], [], [], ['HTTP_ACCEPT' => 'application/json']);
        /** @var FormRequest $formRequest */
        try {
            $formRequest = $class::createFrom($request);
        } catch (ArgumentCountError $e) {
            // Constructor takes dependencies (Laravel would inject them). Resolve
            // each typed parameter from the container; never app()->make() the
            // request itself, because the FormRequest resolving hook validates.
            $args = [];
            foreach ($reflection->getConstructor()->getParameters() as $parameter) {
                $type = $parameter->getType();
                if ($type instanceof ReflectionNamedType && !$type->isBuiltin()) {
                    $args[] = app()->make($type->getName());
                } elseif ($parameter->isDefaultValueAvailable()) {
                    $args[] = $parameter->getDefaultValue();
                } else {
                    throw $e;
                }
            }
            $formRequest = $class::createFrom($request, $reflection->newInstanceArgs($args));
        }
        $formRequest->setContainer(app());
        $formRequest->setRouteResolver(fn () => $route);
        $route->bind($formRequest);
        $raw = $formRequest->rules();
        if (!is_array($raw)) {
            throw new RuntimeException('rules() returned ' . gettype($raw));
        }
        foreach ($raw as $field => $ruleSet) {
            $items = [];
            foreach (explodeRules($ruleSet) as $rule) {
                $item = ruleToJson($rule);
                if (!empty($item['unresolvable'])) {
                    $out['unresolvable'][(string) $field][] = $item['class'] ?? ($item['rule'] ?? 'unknown');
                }
                $items[] = $item;
            }
            $out['rules'][(string) $field] = $items;
        }
    } catch (Throwable $e) {
        $out['error'] = get_class($e) . ': ' . $e->getMessage();
    }
    return $classCache[$cacheKey] = $out;
}

$result = [];
$seen = [];
foreach ($router->getRoutes()->getRoutes() as $route) {
    $uri = $route->uri();
    if (str_starts_with($uri, '__reflect__/')) {
        continue;
    }
    $methods = array_values(array_diff($route->methods(), ['HEAD']));
    $actionName = $route->getActionName();
    $name = $route->getName();
    $key = $name ?: (implode(',', $methods) . ' ' . $uri);
    if (isset($seen[$key])) {
        $seen[$key]++;
        $key .= '#' . $seen[$key];
    } else {
        $seen[$key] = 1;
    }
    $attrKey = implode(',', $route->methods()) . ' ' . $uri . ' ' . $actionName;
    $entry = [
        'uri' => $uri,
        'methods' => $methods,
        'action' => $actionName,
        'name' => $name,
        'middleware' => array_values(array_map(fn ($m) => is_string($m) ? $m : get_class($m), $route->gatherMiddleware())),
        'parameters' => $route->parameterNames(),
        'source' => $attribution[$attrKey] ?? null,
        'validator' => 'none',
        'request_class' => null,
        'request_classes' => [],
        'rules' => new stdClass(),
        'unresolvable' => new stdClass(),
        'rules_depend_on_request' => false,
        'reflection_error' => null,
    ];
    $primaryMethod = $methods[0] ?? 'GET';
    try {
        $callable = null;
        $uses = $route->getAction('uses');
        if ($uses instanceof Closure) {
            $entry['validator'] = 'closure';
            $callable = new ReflectionFunction($uses);
        } elseif (is_string($actionName) && str_contains($actionName, '@')) {
            [$class, $method] = explode('@', $actionName, 2);
            if (!class_exists($class)) {
                throw new RuntimeException("controller class $class does not exist");
            }
            if (!method_exists($class, $method)) {
                throw new RuntimeException("controller method $class@$method does not exist");
            }
            $callable = new ReflectionMethod($class, $method);
        } elseif (is_string($actionName) && class_exists($actionName) && method_exists($actionName, '__invoke')) {
            $callable = new ReflectionMethod($actionName, '__invoke');
        }
        if ($callable) {
            foreach ($callable->getParameters() as $parameter) {
                $type = $parameter->getType();
                if (!$type instanceof ReflectionNamedType || $type->isBuiltin()) {
                    continue;
                }
                $paramClass = $type->getName();
                if (is_a($paramClass, FormRequest::class, true)) {
                    $entry['request_classes'][] = $paramClass;
                }
            }
        }
        if ($entry['request_classes']) {
            $entry['request_class'] = $entry['request_classes'][0];
            $reflected = reflectRequestClass($entry['request_class'], $route, $uri, $primaryMethod, $base);
            $entry['rules'] = (object) $reflected['rules'];
            $entry['unresolvable'] = (object) $reflected['unresolvable'];
            $entry['rules_depend_on_request'] = $reflected['rules_depend_on_request'];
            $entry['request_file'] = $reflected['file'];
            $entry['rules_declared_in'] = $reflected['rules_declared_in'];
            if ($reflected['error']) {
                $entry['validator'] = 'rules-failed';
                $entry['reflection_error'] = $reflected['error'];
            } else {
                $entry['validator'] = $reflected['rules'] ? 'rules' : 'empty';
            }
        }
    } catch (Throwable $e) {
        $entry['validator'] = 'reflection-failed';
        $entry['reflection_error'] = get_class($e) . ': ' . $e->getMessage();
    }
    $result[$key] = $entry;
}
echo json_encode($result, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES);
