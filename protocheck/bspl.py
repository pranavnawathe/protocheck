from protocheck import __version__
from protocheck import logic
from boolexpr import onehot0
from protocheck.precedence import *
from protocheck.bspl_parser import BsplParser
from functools import partial
import itertools
import configargparse
import re
import pprint
import json
import sys
pp = pprint.PrettyPrinter()

def flatten(nested):
    return list(itertools.chain.from_iterable(nested))

class Specification():
    def __init__(self, protocols):
        self.protocols = {}
        self.references = {}
        self.type = 'specification'
        for p in protocols:
            proto = Protocol(p, self)
            self.protocols[proto.name] = proto
        for p in self.protocols.values():
            if p not in self.references:
                self.references[p] = set()
            for r in p.references:
                if r.type != 'protocol': continue
                if r.name in self.protocols:
                    self.references[p].add(self.protocols[r.name])
                else:
                    raise Exception("Reference to unknown protocol: " + r.name)
        for p in self.protocols.values():
            p.resolve_references(self)

def load(definition, path):
    parser = BsplParser(parseinfo=False)
    try:
        protocols = parser.parse(definition, rule_name='document')
        return Specification(protocols)
    except: # catch *all* exceptions
        e = sys.exc_info()[1]
        print("Error in: ", path, file=sys.stderr)
        print(e, file=sys.stderr)
        sys.exit(1)

def load_file(path):
    with open(path, 'r', encoding='utf8') as file:
        raw = file.read()
        raw = strip_latex(raw)

        spec = load(raw, path)
        return spec

class Base():
    """Class containing elements common to protocols, messages, etc."""

    def __init__(self, schema, parent):
        self.schema = schema
        self.parent = parent

    @property
    def name(self):
        return self.schema['name'].strip()

    @property
    def type(self):
        return self.schema['type']

class Reference(Base):
    pass

def reference(schema, parent):
    if schema["type"] == "message":
        return Message(schema, parent)
    elif schema["type"] == "protocol":
        return Reference(schema, parent)
    else:
        raise Exception("Unknown type: " + schema["type"])


class Protocol(Base):
    def __init__(self, schema, parent=None):
        super().__init__(schema, parent)

        self.enactable = None

        self.parameters = {p['name']: Parameter(p, self)
                           for p in schema['parameters']}
        self.private_parameters = {p['name']: Parameter(p, self)
                                   for p in schema.get('private') or []}
        self.keys = {p for p in self.parameters.values()
                     if p.key
                     or parent.type == 'protocol'
                     and p.name in parent.parameters
                     and parent.parameters[p.name].key}
        self.roles = {r['name']: Role(r, self)
                      for r in schema.get('roles', [])}
        self.references = [reference(r, self)
                           for r in schema.get('references', [])]

    def resolve_references(self, spec):
        refs = []
        for r in self.references:
            protocol = spec.protocols.get(r.name)
            if protocol:
                refs.append(protocol.instance(spec,
                                              [self.roles.get(role['name'])
                                               for role in r.schema['params']]))
            else:
                refs.append(r.instance(self))
        self.references = refs

    def instance(self, spec, roles):
        p = Protocol(self.schema, self.parent)
        for i,r in enumerate(self.roles.values()):
            p.roles[r.name] = roles[i]
        p.resolve_references(spec)
        return p

    @property
    def all_parameters(self):
        params = flatten([m.parameters.values() for m in self.messages.values()])

        # don't duplicate parameters
        compact = {}
        for p in params:
            compact[p.name] = p

        return compact.values()

    def _adorned(self, adornment):
        "helper method for selecting parameters with a particular adornment"
        return {p for p in self.parameters.values() if p.adornment == adornment}

    @property
    def ins(self):
        return self._adorned('in')

    @property
    def outs(self):
        return self._adorned('out')

    @property
    def nils(self):
        return self._adorned('nil')

    @property
    def messages(self):
        return {k:v for r in self.references for k,v in r.messages.items()}

    def is_enactable(self):
        if self.enactable is None:
            self.enactable = consistent(logic.And(self.correct, self.enactability))
        return self.enactable

    def is_live(self):
        return self.is_enactable() and not consistent(self.dead_end)
    
    def is_safe(self):
        #prove there are no unsafe enactments
        return not consistent(self.unsafe)

    @property
    def atomicity(self):
        return [logic.And(self.correct,
                          self.maximal,
                          r.enactability,
                          q.incomplete) for q,r in self.refp]

    def check_atomicity(self):
        for formula in self.atomicity:
            s = consistent(formula)
            if s: return s, formula
        return None, None

    def is_atomic(self):
        solution, _ = self.check_atomicity()
        return not solution

    @property
    def refp(self):
        def recur(queue, pairs):
            if not queue: return pairs
            else:
                q,r = queue.pop(0) #get first reference
                if (q, r) not in pairs:
                    pairs.add((q, r))
                    return recur(queue + [(r,s) for s in r.references
                                          if type(s) is not Message or s.is_entrypoint], pairs)
                else:
                    return recur(queue, pairs)

        return recur([(self,q) for q in self.references], set())

    def p_cover(self, parameter):
        if type(parameter) is not str: parameter = parameter.name
        alts = []
        for m in self.messages.values():
            if parameter in m.parameters:
                alts.append(m)
        return alts

    @property
    @logic.named
    def cover(self):
        return logic.And(*[logic.Name(or_(*[m.received for m in self.p_cover(p)]),
                                      p.name + "-cover")
                           for p in self.parameters.values()])

    @property
    @logic.named
    def unsafe(self):
        clauses = []
        for p in self.all_parameters:
            #only out parameters can have safety conflicts
            sources = [m for m in self.p_cover(p.name)
                       if m.parameters[p.name].adornment == 'out']
            if len(sources) > 1:
                alts = []
                for r in self.roles.values():
                    #we assume that an agent can choose between alternative messages
                    msgs = [m.sent for m in sources if m.sender == r]
                    if msgs:
                        alts.append(or_(*msgs))
                # at most one message producing this parameter can be observed at once
                more_than_one = or_(*pairwise(and_, alts))

                # only consider cases where more than one at once is possible
                if more_than_one:
                    clauses.append(logic.Name(more_than_one, p.name + "-unsafe"))
        if clauses:
            #at least one conflict
            return logic.And(self.correct, logic.Name(clauses, "unsafe"))
        else:
            #no conflicting pairs; automatically safe -> not unsafe
            return bx.ZERO

    def _enactable(self):
        "Some message must be received containing each parameter"
        clauses = []
        for p in self.parameters:
            clauses.append(or_(*[m.received for m in self.p_cover(p)]))
        return and_(*clauses)

    @property
    @logic.named
    def enactability(self):
        return self._enactable()

    @property
    @logic.named
    def unenactable(self):
        return ~self._enactable()

    @property
    @logic.named
    def dead_end(self):
        return logic.And(self.correct, self.maximal, self.incomplete)
    
    @property
    @logic.named
    def correct(self):
        clauses = []
        for m in self.messages.values():
            clauses.append(logic.And(m.emission, m.reception, m.transmission, m.non_lossy))
        for r in self.roles.values():
            clauses.append(logic.And(r.nonsimultaneity(self), r.minimality(self)))
        return logic.And(*clauses)

    @property
    @logic.named
    def maximal(self):
        "Each message must be sent, or it must be blocked by a prior binding"
        clauses = []
        for m in self.messages.values():
            clauses.append(m.sent | m.blocked)
        return and_(*clauses)

    @property
    @logic.named
    def begin(self):
        return or_(*[m.sent for m in self.messages.values()])

    def _complete(self):
        "Each out parameter must be observed by at least one role"
        clauses = []
        for p in self.outs:
            clauses.append(or_(*[m.received for m in self.p_cover(p) \
                                 if m.parameters[p.name].adornment is 'out']))
        return and_(*clauses)

    @property
    @logic.named
    def complete(self):
        return self._complete()

    @property
    @logic.named
    def incomplete(self):
        return ~self._complete()

    @property
    def is_entrypoint(self):
        "A protocol is an entry point if it does not have any \
        dependencies on sibling protocols"
        return not self.ins - self.parent.ins


class Message(Protocol):
    def __init__(self, schema, parent):
        super().__init__(schema, parent)
        self.sender = parent.roles.get(schema['sender'])
        if not self.sender:
            raise LookupError("Role not found", schema['sender'])
        self.recipient = parent.roles.get(schema['recipient'])
        if not self.recipient:
            raise LookupError("Role not found", schema['recipient'])

        for p in self.parameters:
            if p not in parent.parameters \
               and p not in parent.private_parameters:
                raise LookupError("Undeclared parameter", p)

    @property
    def name(self):
        return self.parent.name + "/" + self.schema['name']

    def instance(self, parent):
        return Message(self.schema, parent)

    @property
    def messages(self):
        return {self.name: self}

    @property
    def sent(self):
        return send(self.sender, self.name)

    @property
    def received(self):
        return recv(self.recipient, self.name)

    @property
    @logic.named
    def enactability(self):
        return self.received

    @property
    @logic.named
    def unenactable(self):
        return ~self.received

    @property
    def blocked(self):
        s = partial(observe, self.sender)
        nils = [and_(s(p), ~(sequential(s(p), self.sent) | simultaneous(s(p), self.sent))) for p in self.nils]
        outs = [s(p) for p in self.outs]
        return or_(*(nils + outs))

    @property
    @logic.named
    def transmission(self):
        "Each message reception is causally preceded by its emission"
        return impl(self.received, sequential(self.sent, self.received))

    @property
    @logic.named
    def non_lossy(self):
        "Each message emission results in reception"
        return impl(self.sent, self.received)

    @property
    @logic.named
    def emission(self):
        """Sending a message must be preceded by observation of its ins,
           but cannot be preceded by observation of any nils or outs"""
        s = partial(observe, self.sender)
        ins = [impl(self.sent, sequential(s(p), self.sent))
               for p in self.ins]
        nils = [impl(and_(self.sent, s(p)), sequential(self.sent, s(p)))
                for p in self.nils]
        outs = [impl(self.sent, simultaneous(s(p), self.sent))
                for p in self.outs]
        return and_(*(ins + nils + outs))

    @property
    @logic.named
    def reception(self):
        "Each message reception is accompanied by the observation of its parameters; either they are observed, or the message itself is not"
        clauses = [impl(self.received,
                        or_(sequential(p, self.received),
                            simultaneous(p, self.received)))
                   for p in map(partial(observe, self.recipient), self.ins | self.outs)]
        return and_(*clauses)

class Role(Base):
    def messages(self, protocol):
        return {m.name:m for m in protocol.messages.values()
                if m.sender == self or m.recipient == self}

    def observe(self, msg):
        return observe(self.name, msg)

    def sent_messages(self, protocol):
        return [m for m in protocol.messages.values() if m.sender == self]

    @logic.named
    def minimality(self, protocol):
        "Every parameter observed by a role must have a corresponding message transmission or reception"
        sources = {}
        def add(m, p):
            p = p.name
            if p in sources:
                sources[p].append(m)
            else:
                sources[p] = [m]

        for m in self.messages(protocol).values():
            if m.recipient == self:
                for p in m.ins:
                    add(m, p)
            for p in m.outs:
                add(m, p)

        return and_(*[impl(self.observe(p),
                           or_(*[simultaneous(self.observe(m), self.observe(p)) for m in sources[p]]))
                     for p in sources])

    @logic.named
    def nonsimultaneity(self, protocol):
        msgs = [m.sent for m in protocol.messages.values() if m.sender == self]
        if len(msgs) > 1:
            return ordered(*msgs)
        else:
            return bx.ONE

class Parameter(Base):
    def __init__(self, schema, parent=None):
        super().__init__(schema, parent)
        self.key = schema['key']

    @property
    def adornment(self):
        return self.schema['adornment']

@wrap(name)
def observe(role, event):
    return var(role + ":" + event)

send = observe
recv = observe

def strip_latex(spec):
    """Remove all instances of '\mapsto' and '\msf{}' from a latex listing, to make it proper BSPL"""
    spec = re.sub(r'\$\\msf{(\w+)}\$', r'\1', spec)
    spec = re.sub(r'\$\\mapsto\$', r'->', spec)
    return spec


def print_formula(*formulas):
    print("\nFormula:")
    print(json.dumps(logic.merge(*formulas),
                     default=str, sort_keys=True, indent=2))
    print()


def handle_enactability(protocol, args):
    reset_stats()
    e = protocol.is_enactable()
    print("  Enactable: ", bool(e))
    if args.verbose or args.stats:
        print("    stats: ", stats)
    if not e and not args.quiet or args.verbose:
        print_formula(logic.And(protocol.correct, protocol.enactability))
    if e and args.verbose:
        pp.pprint(e)

    return e


def handle_liveness(protocol, args):
    reset_stats()
    e = protocol.is_enactable()
    violation = consistent(protocol.dead_end)
    print("  Live: ", e and not violation)
    if args.verbose or args.stats:
        print("    stats: ", stats)
    if violation and not args.quiet or args.verbose:
        print_formula(protocol.dead_end)
    if violation and not args.quiet:
        print("\n    Violation:")
        pp.pprint(violation)
        print()


def handle_safety(protocol, args):
    reset_stats()
    expr = protocol.unsafe
    violation = consistent(expr)
    print("  Safe: ", not violation)
    if args.verbose or args.stats:
        print("    stats: ", stats)
    if violation and not args.quiet or args.verbose:
        print_formula(expr)
    if violation and not args.quiet:
        print("\nViolation:")
        pp.pprint(violation)
        print()


def handle_atomicity(protocol,args):
    reset_stats()
    a, formula = protocol.check_atomicity()
    print("  Atomic: ", not a)
    if args.verbose or args.stats:
        print("    stats: ", stats)
    if args.verbose:
        print_formula(*protocol.atomicity)
    if a and not args.quiet:
        print("\nViolation:")
        pp.pprint(a)
        print("\nFormula:")
        print(json.dumps(formula, default=str, sort_keys=True, indent=2))
        print()


def handle_all(protocol, args, **kwargs):
    enactable = handle_enactability(protocol, args, **kwargs)
    if enactable:
        handle_liveness(protocol, args, **kwargs)
        handle_safety(protocol, args, **kwargs)
        handle_atomicity(protocol, args, **kwargs)


def check_syntax(*args):
    print("Syntax: correct")


def main():
    actions = {
        'enactability': handle_enactability,
        'liveness': handle_liveness,
        'safety': handle_safety,
        'atomicity': handle_atomicity,
        'syntax': check_syntax,
        'all': handle_all
    }

    parser = configargparse.get_argument_parser()
    parser.description = 'BSPL Protocol property checker'
    parser.add('-s', '--stats', action="store_true",
               help='Print statistics')
    parser.add('-v', '--verbose', action="store_true",
               help='Print additional details: spec, formulas, stats, etc.')
    parser.add('-q', '--quiet', action="store_true",
               help='Prevent printing of violation and formula output')
    parser.add('-f', '--filter', default='.*',
               help='Only process protocols matching regexp')
    parser.add('--version', action="store_true", help='Print version number')
    parser.add('action', help='Primary action to perform',
               choices=actions.keys())
    parser.add('input', nargs='+', help='Protocol description file(s)')

    if '--version' in sys.argv:
        print(__version__)
        sys.exit(0)
    else:
        args = parser.parse()

    for path in args.input:
        spec = load_file(path)
        for protocol in spec.protocols.values():
            if re.match(args.filter, protocol.name):
                print("%s (%s): " % (protocol.name, path))
                actions[args.action](protocol, args)
                print()

    if not spec.protocols:
        print("No protocols parsed from file: ", args.input)


if __name__ == "__main__":
    main()
