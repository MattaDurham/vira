"""Species-informed axial anatomy for the koi's deformation study.

Reference: Al Fatle & Jawad (2026), doi:10.26842/binhm.7.2026.19.1.0139,
plates 2-4 and figure 1. This models a 32-centrum example within the observed
variation, not a CT reconstruction. Adult teleost joints are not mammalian
cartilage discs: the central connective bridge represents notochord tissue.
"""

import math


def spine_height(x):
    # The carp's vertebral axis arches through the anterior trunk and slopes
    # towards the peduncle, rather than running down an ellipsoid's centre.
    return .018+.058*math.exp(-((x+.16)/.26)**2)-.018*max(0,min(1,(x-.25)/.29))


def vertebral_positions():
    lengths = [.020]*4+[.023]*6+[.031]*18+[.029]*4
    scale = .94/sum(lengths)
    positions = [-.67,-.40]
    for length in lengths:
        positions.append(positions[-1]+length*scale)
    return positions+[.84]


def author_anatomy(objects, mesh, curve, sphere, material, interpolate, surface, positions):
    import bpy

    bone_mat = material("Anatomy mineralized bone", (.54,.67,.61), .55)
    chord_mat = material("Anatomy notochord tissue", (.21,.48,.35), .46)
    cartilage = material("Anatomy radial cartilage", (.34,.59,.50), .40)

    def unite(pieces, name, joint):
        bpy.ops.object.select_all(action="DESELECT")
        for obj in pieces:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = pieces[0]
        bpy.ops.object.join()
        combined = bpy.context.object
        combined.name = name
        combined["rigid_joint"] = joint
        objects[:] = [obj for obj in objects if obj not in pieces]+[combined]

    start=len(objects)
    facial=[]
    vertices,faces=[],[]
    for i in range(15):
        x=-.57+.185*i/14
        for j in range(17):
            vertices.append(surface(x,.2+(math.pi-.4)*j/16,-.012))
    for i in range(14):
        for j in range(16):
            a=i*17+j;faces.append((a,a+1,a+18,a+17))
    mesh("Anatomy cranial roof",vertices,faces,[bone_mat])
    for side in [-1,1]:
        ramus=curve("Anatomy mandibular ramus",[(-.654,side*.020,-.042),(-.598,side*.055,-.083),(-.49,side*.064,-.070)],.003,bone_mat)
        ramus["facial_joint"]="jaw_lower";facial.append(ramus)
        curve("Anatomy orbital rim",[(-.534+.024*math.cos(math.tau*i/20),side*.077,.0035+.024*math.sin(math.tau*i/20)) for i in range(21)],.0015,bone_mat)
        vertices,faces=[],[]
        for i in range(13):
            t=i/12;angle=(-.92+1.96*t) if side>0 else (math.pi+.92-1.96*t)
            rear=-.374-.039*abs(2*t-1)
            for j in range(9):
                vertices.append(surface(-.49+(rear+.49)*j/8,angle,-.008))
        for i in range(12):
            for j in range(8):
                a=i*9+j;f=(a,a+1,a+10,a+9);faces.append(f if side>0 else tuple(reversed(f)))
        operculum=mesh("Anatomy opercular plate",vertices,faces,[bone_mat])
        operculum["facial_joint"]="operculum_L" if side>0 else "operculum_R";facial.append(operculum)
        for i in range(5):
            sphere("Anatomy pharyngeal tooth",(-.44+i*.009,side*(.018+i*.004),-.098-i*.002),(.004,.004,.0055),bone_mat,segments=12)
    premaxilla=curve("Anatomy premaxillary arch",[(-.653,-.030,-.025),(-.646,0,-.017),(-.653,.030,-.025)],.0025,bone_mat)
    premaxilla["facial_joint"]="jaw_upper";facial.append(premaxilla)
    unite([obj for obj in objects[start:] if obj not in facial],"Anatomy cranial complex",0)

    for index in range(1,33):
        start = len(objects)
        a,b = positions[index],positions[index+1]
        x = (a+b)/2
        z = spine_height(x)
        width,upper,lower,center = interpolate(x)
        radius = .006+.011*math.exp(-((x+.18)/.37)**2)
        length = (b-a)*.78
        vertices,faces = [],[]
        rings,sides = 8,20
        # Two hollow chordal cones meet at a small central passage, inside a
        # cylindrical bony centrum. Neither the bone nor its spines stretch.
        for inner in [False,True]:
            for i in range(rings+1):
                t=i/rings-.5
                r=radius*((.12+.66*(2*abs(t))**1.3) if inner else (.88+.12*(2*abs(t))**2))
                for j in range(sides):
                    angle=math.tau*j/sides
                    vertices.append((x+t*length,r*math.cos(angle),z+r*math.sin(angle)))
        stride=(rings+1)*sides
        for layer in range(2):
            for i in range(rings):
                for j in range(sides):
                    a0=layer*stride+i*sides+j
                    b0=layer*stride+i*sides+(j+1)%sides
                    face=(a0,b0,b0+sides,a0+sides)
                    faces.append(face if layer==0 else tuple(reversed(face)))
        for end in [0,rings]:
            for j in range(sides):
                a0=end*sides+j;b0=end*sides+(j+1)%sides
                face=(a0,a0+stride,b0+stride,b0)
                faces.append(face if end==0 else tuple(reversed(face)))
        mesh(f"Anatomy centrum {index:02}",vertices,faces,[bone_mat])
        curve("Anatomy neural arch",[(x,-radius*.67,z+radius*.5),(x,0,z+radius*1.9),(x,radius*.67,z+radius*.5)],radius*.11,bone_mat)
        neural_top=max(z+radius*2.0,center+upper-.023)
        curve("Anatomy neural spine",[(x,0,z+radius*1.65),(x+length*.35,0,neural_top)],radius*.09,bone_mat)
        if 5<=index<=17:
            for side in [-1,1]:
                curve("Anatomy abdominal rib",[(x,side*radius,z-radius*.3),
                      (x+length*.35,side*width*.78,center-lower*.30),
                      (x+length*.65,side*width*.46,center-lower+.025)],radius*.09,bone_mat)
        elif index>=18:
            curve("Anatomy haemal arch",[(x,-radius*.65,z-radius*.5),(x,0,z-radius*1.9),(x,radius*.65,z-radius*.5)],radius*.11,bone_mat)
            bottom=min(z-radius*2,center-lower+.025)
            curve("Anatomy haemal spine",[(x,0,z-radius*1.65),(x+length*.55,0,bottom)],radius*.09,bone_mat)
        unite(objects[start:],f"Anatomy vertebra {index:02}",index)
        if index<32:
            curve(f"Anatomy notochord bridge {index:02}",[(x+length*.40,0,z),
                  (positions[index+1]+(positions[index+2]-positions[index+1])*.11,0,spine_height(positions[index+1]))],radius*.22,chord_mat)

    # Six hypurals, split into upper and lower fans at the terminal complex.
    vertices,faces=[],[]
    for i in range(6):
        lo=-.068+i*.023
        k=len(vertices)
        vertices += [(.515,-.001,spine_height(.515)),(.58,-.001,lo),(.59,-.001,lo+.018),
                     (.515,.001,spine_height(.515)),(.58,.001,lo),(.59,.001,lo+.018)]
        faces += [(k,k+1,k+2),(k+5,k+4,k+3),(k,k+3,k+4,k+1),(k+1,k+4,k+5,k+2)]
    terminal=mesh("Anatomy hypural complex",vertices,faces,[bone_mat]);terminal["rigid_joint"]=32
    for i in range(20):
        x=-.08+.49*i/19
        _,upper,_,center=interpolate(x)
        top=center+upper
        support=curve("Anatomy dorsal pterygiophore",[(x-.065,0,spine_height(x-.065)+.020),
              (x,0,top-.012)],.0011,bone_mat)
        support["rigid_joint"]=min(range(1,33),key=lambda j:abs((positions[j]+positions[j+1])/2-(x-.065)))
        curve("Anatomy dorsal radial joint",[(x,0,top-.014),(x,0,top-.008)],.002,cartilage)
